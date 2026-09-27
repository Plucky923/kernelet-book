# Zero-copy I/O

*The second version of devices: a paravirtual interface in which a kernelet lends frames to the host for the life of one request, so that bulk data moves between a tenant's memory and a physical device without the host copying it. Nothing on this page has been built on either host; it is specified so that the first version does not close the door on it.*

## When a copy matters

The [first version](virtualizing-ostd/devices.md) of devices copies every byte once between a kernelet's buffer and Linux's. Whether that matters depends on the request. *Measured here* (the development machine, user space, one run): copying a 1,500-byte packet costs 14 ns when the data is in cache and 231 ns when it is not; a 4 KiB page, 33 and 457 ns; waking a thread on another processor, about 3 µs.

So for small requests the copy is noise beside the wakeup, and a "zero-copy" path that still wakes a thread per request saves nothing. For bulk transfer the copy is the bill: one processor copies about 10 GB/s of cold data, so a sandbox streaming 10 GB/s from disk costs the host a core for copying alone. The second version is for that case. Its measure of success is host processor time per request as a function of request size: a fixed part (crossings, wakeups, checks) and a per-byte part (copies), and it aims to make the per-byte part zero.

## Lending

A **lending device** is a device to which a kernelet lends frames. It has a submit ring and a complete ring in the kernelet's own memory, as virtio has, and the endovisor reaches them only through the grant check. An entry on the submit ring names up to sixteen of the kernelet's *frames*, by physical address, lent to the device until the request completes. Three rules carry the design.

1. **A lent frame is still the tenant's, but frozen.** The kernelet's driver holds a reference to each lent frame until the completion arrives, so the kernel proper cannot free or reuse it; and the endovisor marks the frame's grain as *lent* in the owner array, so that [destroy](faults-and-reclamation.md#destroy) refuses to return the grain to Linux while a device may still write to it. The protection is enforced twice, once on each side of the boundary, because a failure here would let a physical device write into memory that has been given to someone else.
2. **The host acts only on bytes it has copied out.** A submit entry is read whole into endovisor memory and validated there. Anything the endovisor, Linux or a receiving sandbox later interprets (an opcode, a sector number, a packet header) is that private copy, so the tenant cannot change it after the check.
3. **No host thread on the fast path.** Submission happens inside the notify service call, bounded and without sleeping. Completion comes from Linux's own completion path, in interrupt context, and does only what is legal there: mark the frames returned, write a completion entry through the direct map, raise the kernelet's virtual interrupt.

## What Linux offers this design

On the Asterinas host the block layer has to be given a way to accept a request over frames it did not allocate, without sleeping. Linux already has one.

- **Block.** The endovisor builds a Linux block request (a `bio`) whose segments are the lent frames themselves, with [`bio_add_page()`](https://elixir.bootlin.com/linux/v6.12/source/block/bio.c#L1132), and submits it with [`submit_bio()`](https://elixir.bootlin.com/linux/v6.12/source/block/blk-core.c#L886) and the flag [`REQ_NOWAIT`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/blk_types.h#L406), which makes Linux fail the request instead of sleeping when it has no room. The grant's frames are ordinary pages from Linux's page allocator, so they are valid DMA targets. The physical device reads from or writes to the tenant's frame, and the `bio`'s completion callback is the completion path of rule 3. When the backing store is a file rather than a block device, the same frames go to the file system as direct I/O, which is how Linux's loop driver serves a file-backed disk ([`lo_rw_aio()`](https://elixir.bootlin.com/linux/v6.12/source/drivers/block/loop.c#L410)).
- **Network transmit.** Linux's sockets accept pages to be spliced into a packet without copying ([`MSG_SPLICE_PAGES`](https://elixir.bootlin.com/linux/v6.12/source/include/linux/socket.h#L332)), and the lent frames can be those pages. The header Linux sends is the endovisor's validated copy.
- **Network receive** is zero-copy only if the network card can steer a sandbox's packets into that sandbox's own buffers, which needs a receive queue per sandbox. Without one, receive costs one copy, as in the first version.
- **Between sandboxes**, one copy, frame to frame, in the sender's notify call; the two-copy path through the [switch](channels.md) is avoided but the rule that no frame is visible to two tenants is not relaxed.

**The IOMMU.** On a machine that confines devices with an IOMMU, Linux's DMA interface maps a request's pages for the device as part of submitting the `bio`. The endovisor does not manage IOMMU mappings itself. That is simpler than on the Asterinas host, where the design maps a kernelet's grains into the host's IOMMU domain when they are granted.

## What is open

All of it is **[unverified]**. The two assumptions with the most weight are that `REQ_NOWAIT` submission fails cleanly, rather than sleeping, on every block driver an operator would put under a sandbox, and that a per-sandbox receive queue can be had on the network hardware that matters.

## The rings, for completeness

The ring layout does not depend on the host. It is given here so that this page can be implemented alone; the [Asterinas host's version](../asterinas-mode/zero-copy-io.md) argues each choice.

A lending device has a **submit ring** and a **complete ring**, each a power-of-two array of at most 256 fixed-size entries in the kernelet's grant, registered once with a service the second version adds, `dev_ring_set(dev, submit_paddr, complete_paddr, entries)`, which is refused while any request is in flight. The kernelet writes the submit ring and reads the complete ring; the endovisor does the reverse; each side publishes its counter in the first entry of the ring it writes. A second added service, `dev_notify(dev)`, takes no pointer and is the only doorbell.

```c
struct klet_seg    { uint64_t paddr; uint16_t off; uint32_t len; };   /* contiguous frames inside one grain */
struct klet_submit {                                                   /* 536 bytes */
        uint32_t id;          /* below the ring size, unique among requests in flight, echoed back */
        uint16_t op;          /* READ, WRITE, FLUSH, TX, RX_POST */
        uint16_t nseg;        /* 1..16 segments lent; 0 for a header-only packet */
        uint64_t sector;
        uint16_t hdr_len, csum_start, csum_off, gso_size;
        uint8_t  hdr[256];    /* the endovisor acts on its own copy of this, never on the ring */
        struct klet_seg seg[16];
};
struct klet_complete { uint32_t id; uint16_t status; uint16_t _pad; uint64_t bytes; };
```

An entry carries at most sixteen pages, 64 KiB; larger requests are split. On notify, the endovisor copies each new entry out, validates the copy (every segment inside one grain of this kernelet, `id` unused, lengths within bounds), marks the grains lent, and submits; at most 32 entries per notify, so the call is bounded. To suppress interrupts under load, the kernelet sets one polling bit in its ring header while it is draining completions, and both sides re-check after setting or clearing it, so that a completion is never left unannounced.

## What this page decides

- **The second version's devices are lending devices** (register D70 to D72, kept), enforced on both sides of the boundary.
- **On Linux, a lent frame is handed to the block layer as a `bio` segment with `REQ_NOWAIT`, and to a socket as a spliced page** (register D109), in place of the non-sleeping submission path the other host must grow (assumption A14).
