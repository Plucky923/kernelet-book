# Zero-copy I/O: allocation lifetime and access grants

## Problem {#problem}

The host performs device I/O for kernelets and mediates inter-kernelet communication.
This proposal aims to eliminate payload copies between the host and kernelets and between kernelets.
Successive producers and consumers access the same underlying storage to avoid these copies. [Issue #5](https://github.com/tatetian/kernelet-book/issues/5)

Shared kernel mappings make buffer storage addressable across participants.
Trusted interfaces must enforce each holder's authorized range, read/write permissions, and buffer lifetime.
The [threat model](../paper/background.md) permits logic errors in memory-safe kernelet code.
These interfaces must preserve memory safety for every use permitted by their safe APIs.

## Current design {#current-design}

### Existing I/O paths {#existing-paths}

The initial Asterinas block backend uses `read_at` to copy data into kernelet-provided buffers.
The design describes this path as operating "with the host's page cache as the intermediate". [Device design](../blueprint/asterinas-mode/virtualizing-ostd/devices.md)
Initial inter-kernelet channels copy payloads into a host queue, then into receiver-posted buffers.
The channel design states that "no frame changes owner, no pointer is carried". [Channel design](../blueprint/design/channels.md)
Both paths deliver bytes into destination buffers without transferring access to the source storage.

For supported backends, lending lets host drivers use kernelet-owned frames directly for device I/O.
The host prevents frame reclamation until the request completes.
It still copies and validates request metadata and any headers it interprets.
[Asterinas lending design](../blueprint/asterinas-mode/zero-copy-io.md#the-idea), [Linux lending design](../blueprint/linux-mode/zero-copy-io.md)

| Path | Payload movement | Payload copy |
|---|---|---|
| Block I/O with direct DMA | Device ↔ lent kernelet frames | Avoided |
| Buffered file backend (`read_at`) | Host page cache ↔ kernelet buffer | Remains |
| Network transmit using lent frames | NIC reads lent payload frames | Avoided |
| Network receive with a dedicated, steered queue | NIC writes into lent kernelet frames | Avoided |
| Network receive with a shared queue | Host receive buffer → kernelet buffer | One copy |
| Inter-kernelet channel (`lend-chan`) | Sender frame → receiver-posted frame | One copy |

The Blueprint specifies these [block](../blueprint/asterinas-mode/zero-copy-io.md#block), [network](../blueprint/asterinas-mode/zero-copy-io.md#network), and [channel](../blueprint/asterinas-mode/zero-copy-io.md#between-kernelets) paths.

### Lending already tracks ownership and borrowing {#lending-contract}

The lending design uses driver-held references and host lend counts to protect buffer lifetime.
The kernelet driver retains each submitted request and its buffers until completion.
Before submission, the host atomically checks grain ownership and increments its lend count.
A [grain](../blueprint/asterinas-mode/virtualizing-ostd/memory.md) is the host's memory-grant unit; a nonzero lend count prevents its reclamation or reassignment.
[Lending contract](../blueprint/asterinas-mode/zero-copy-io.md#lending)

The lending contract permits tenant access during I/O: "its processes may read and write it."
The host bases its decisions on copied control information and interpreted headers.
[Lending contract](../blueprint/asterinas-mode/zero-copy-io.md#lending)

Lending already tracks ownership and borrowing through grain ownership, lend counts, and driver-held references.
The host's lend counts track grains; RedLeaf tracks ownership and borrowing of shared objects.
Both mechanisms retain storage while another participant uses it.
[Issue #5](https://github.com/tatetian/kernelet-book/issues/5)

In `lend-chan`, receiver B posts buffer Y.
The host "copies the payload once, frame to frame" from sender A's buffer X into Y.
Eliminating this copy requires B to accept access to X and release that access after use.
Trusted interfaces must retain X and enforce each participant's authorized range and access mode throughout the handoff.
[Channel design](../blueprint/asterinas-mode/zero-copy-io.md#between-kernelets)

The channel design also sketches a frame-move extension.
It "takes a whole grain out of the sender's grant and appends it to the receiver's".
The move rewrites grain ownership and both grant tables.
Its prerequisites include a grain-filling payload, no sender user mappings of the transferred frames, and notification that the receiver's grant has grown.
[Frame-move extension](../blueprint/design/channels.md)

## Proposed approach {#proposed-approach}

Adapt RedLeaf's shared heap and ownership tracking to host-managed I/O byte buffers.
Transfer or lend access to the same buffer between the host and kernelets or between kernelets.
Trusted interfaces must enforce allocation bounds and read/write permissions.
They must retain each allocation until all grants and backend accesses end.

Lending already retains submitted frames through driver-held references and host lend counts.
Preserve this lifetime rule while adding explicit access grants for individual buffers.
Consuming local wrappers end access before submission or handoff.
A channel receiver imports the original buffer instead of receiving a payload copy.
Reuse lending's request validation, bounded queues, and device completion handling.

| Contract | Existing lending design | Proposed shared buffers |
|---|---|---|
| Storage | Kernelet-owned frames | Shared byte buffers from a host-managed pool |
| Lifetime | Driver-held references retain frames; host lend counts retain grains | Allocation holds cover issued grants, active wrappers, backend operations, and callbacks |
| Submitter access during I/O | Tenant processes may read and write lent frames | No local access until a return grant is redeemed |
| Channel receive | B posts Y; the host copies A's payload from X into Y | B redeems a grant for X and accesses the same storage |
| Receiver credit | Credit follows receiver-posted frames | Credit bounds imported buffer count and retained bytes |

The existing frame-move extension transfers a whole grain between tenant memory grants.
This proposal keeps backing storage in a host-managed pool and transfers access grants for individual buffers.
Ordinary tenant memory grants do not change during buffer handoff.
[Existing frame-move extension](../blueprint/design/channels.md)

### Reuse the shared heap and adapt the exchange contract {#typed-exchange}

RedLeaf allocates exchangeable objects on a shared heap.
Its paper describes a "special shared heap that allows allocation of objects that can be exchanged between domains."
Trusted proxies update ownership and borrowing records when objects cross domain boundaries.
The receiving domain accesses the original allocation.
[Source](https://www.usenix.org/system/files/osdi20-narayanan_vikram.pdf#page=3)

RedLeaf's interface compiler restricts exchangeable types to preserve heap isolation.
The paper requires that "objects allocated on the shared heap cannot have pointers into private domain heaps."
Shared objects may reference other shared objects through `RRef<T>`.
This restriction prevents private-heap reclamation from leaving dangling pointers inside transferred objects.
[Source](https://www.usenix.org/system/files/osdi20-narayanan_vikram.pdf#page=3)

The current cross-image ABI does not define a contract for exchanging arbitrary `RRef<T>` objects.
This proposal exchanges bounded byte buffers using identifiers and operation parameters.
Trusted host state retains each allocation and records authorized holders, ranges, and read/write permissions.
The receiving vOSTD constructs a local wrapper from a validated grant.
Local wrappers expose byte or POD operations.
Typed object exchange would require agreement on type identity, representation, validity, ownership, and destruction.
[Cross-image ABI](../blueprint/asterinas-mode/builds-and-images.md#entry), [Issue #5](https://github.com/tatetian/kernelet-book/issues/5)

Byte-buffer exchange must preserve Rust's aliasing and lifetime requirements.
A faulty trusted interface could give A and B independent mutable references to the same buffer.
Conflicting accesses through these references while both remain live can cause undefined behavior.
Trusted interfaces must end conflicting access before granting a new holder access to the same range.
[Rust Reference](https://doc.rust-lang.org/reference/behavior-considered-undefined.html#undefined.alias)

A buffer descriptor identifies a registered buffer and a range within it.
Passing the descriptor leaves the payload in its original storage.
The host validates the recipient's grant; vOSTD constructs a local wrapper that exposes only the authorized access.
[Issue #5](https://github.com/tatetian/kernelet-book/issues/5)

### Allocate shared buffers and retain their storage {#buffer-record}

The first prototype uses a host-managed pool of shared byte buffers.
Producers write payloads directly into these buffers.
Consumers access the same storage through validated local wrappers.
Handoff avoids payload copying when all participants and the backend can use the original storage.

Host OSTD or the Linux host adapter owns the pool's backing storage, outside ordinary tenant memory grants.
The prototype hands off whole buffers to one authorized participant at a time.
One participant may create multiple local readers from a read-only wrapper.
The prototype excludes user mappings, unrestricted frame handles, and overlapping subrange grants.
Read sharing across participants requires a protocol extension.

The host keeps three kinds of trusted records:

| Record | Required information | Purpose |
|---|---|---|
| Allocation | Buffer ID and generation; backing allocation; capacity, initialized range, and payload length; accounting owner; storage holds | Retain storage and validate bounds |
| Grant | Grant ID and generation; buffer identity; holder; range; read/write mode; issued, active, or ended state | Authorize one holder and construct its local wrapper |
| Operation | Request ID and generation; retained buffer; backend read/write mode; result; outstanding accesses | Retain storage through asynchronous work and reject stale completions |

Each identifier includes a generation to distinguish a reused slot from its previous occupant.

A storage hold prevents an allocation from being reclaimed.
Issuing a grant creates a hold before the recipient redeems it.
Redemption activates the grant and constructs one local wrapper while preserving the hold.
An active grant keeps its hold until release or an accepted transition ends the grant.
Every backend operation or callback that can access the buffer also retains a hold.
The host reclaims storage only after all holds end, including those for grants that recipients never redeemed.

The prototype zeroes new or recycled buffers before granting local access.
This initializes the full capacity and removes data from previous allocations.
Payload length records the useful data and must fit within the authorized range.
Handoff preserves the existing payload without clearing it.
Skipping zeroing would require tracking initialized ranges before exposing bytes through a reader.

The original requester remains the allocation's accounting owner until reclamation.
Receiver credit limits the number and retained bytes of imported buffers.
If the accounting owner terminates, the host retains its charge record until the allocation is reclaimed.
The host reclaims byte storage without invoking destructors in a terminated kernelet image.

### Transfer access through checked local wrappers {#access-transitions}

vOSTD redeems each grant through the host before constructing a local buffer wrapper.
The host identifies the caller from trusted service context.
It checks buffer and grant identities, generations, holder, range, access mode, and issued state.
Successful redemption atomically changes the grant from issued to active.
vOSTD then constructs exactly one wrapper with the validated permissions.
Repeated redemption, stale identifiers, and calls from other sandboxes fail without changing the grant.
[Service context](../blueprint/asterinas-mode/kernelet-api-service.md)

Read-only grants produce `ReadBuffer`, which exposes only `reader()`.
Its safe API provides no way to construct a writer for the buffer.
Read/write grants produce `ReadWriteBuffer`, which also exposes `writer(&mut self)`.
The following signatures illustrate local accessor rules; they do not cross the C ABI:

```text
ReadBuffer::reader(&self) -> VmReader<'_, Infallible>
ReadWriteBuffer::reader(&self) -> VmReader<'_, Infallible>
ReadWriteBuffer::writer(&mut self) -> VmWriter<'_, Infallible>
```

These buffer types represent shared byte storage and enforce local read/write permissions.
Device request and queue abstractions can build on them.
Trusted host backends manage DMA mappings and device synchronization.
Higher-level interfaces must preserve the buffers' access and lifetime rules.

Readers expose only initialized bytes within the authorized range.
Writers require an exclusive local borrow and stay within that range.
Wrappers expose no backing handle that bypasses these permissions.
Safe APIs cannot duplicate owning wrappers through `Clone`, `Copy`, or unchecked construction from an identifier.
The first prototype provides no buffer splitting operation.

An active wrapper enforces its validated permissions locally.
Creating readers or writers requires no additional host call.
Submission, handoff, and release consume the wrapper after all accessor borrows end.
vOSTD retains the wrapper while the host decides whether to accept the operation.
Rejection returns the wrapper with its original permission; acceptance ends its grant.

The host serializes grant redemption, transfer, release, completion, and teardown.
Changing a grant record cannot invalidate an existing reader or writer.
Trusted interfaces must end conflicting CPU or DMA access before granting new access.
C ABI calls carry identifiers, generations, ranges, modes, and operations.
The host validates these values against trusted records and the caller's identity.

### Protocol operations and states {#protocol-states}

The following states track access to one whole buffer.
`P` denotes a host participant or kernelet, and `op` denotes one accepted backend operation.
`Issued(P)` means P has an unredeemed grant; `Local(P)` means P holds the corresponding local wrapper.
`Backend(op)` means the backend holds the buffer for operation `op`.
The host manages the backing allocation throughout these transitions.

| Event | Before | After | Required condition |
|---|---|---|---|
| Allocate | Free pool storage | `Issued(P)` | Reserve quota and storage; initialize bytes; create grant |
| Redeem | `Issued(P)` | `Local(P)` | Authenticate P; validate and activate the grant exactly once |
| Submit I/O | `Local(P)` | `Backend(op)` | End accessors; validate request; install retained operation record before publishing work |
| Return from I/O | `Backend(op)` | `Issued(P)` | End backend access; finish required DMA synchronization; P remains eligible to receive the buffer |
| Hand off | `Local(A)` | `Issued(B)` | End A's accessors; validate connection, range, and mode; reserve B's credit |
| Release | `Local(P)` | Await reclamation | End local accessors and the grant |
| Discard grant | `Issued(P)` | Await reclamation | Cancel the unredeemed grant |
| Reclaim | Await reclamation | Free pool storage | All grant, backend, and callback holds have ended; retire stale identities |

Redemption activates a grant; release, discard, or accepted transfer ends it.
Every new grant receives a fresh identity, including a return grant for the original holder.
Reusing an allocation, grant, or request record slot advances its generation.
The host rejects identifiers whose generation does not match the current record.
A slot must be retired before generation wraparound could make a stale identifier valid again.

The following signatures describe local operations and results, not the C ABI layout.
Each buffer wrapper is a `ReadBuffer` or `ReadWriteBuffer`, selected by its validated grant.

```text
allocate(capacity)
    -> ReadWriteBuffer | Error(error)

submit(buffer, io)
    -> Accepted(request) | Rejected(buffer, error)

handoff(buffer, peer, mode, payload_len)
    -> Accepted | Rejected(buffer, error)

receive(channel)
    -> Pending | Received(buffer) | Error(error)

poll_return(request)
    -> Pending | Returned(status, buffer) | Error(error)

cancel(request)
    -> Requested | AlreadyFinished | Error(error)

release(buffer)
    -> ()
```

Accepted submission ends the caller's grant; a new return grant restores local access after backend access ends.
Accepted handoff ends the sender's grant; the receiver redeems its own grant.
Rejection preserves the original grant and returns the original wrapper exactly once.
Errors after acceptance are operation results and cannot restore the original wrapper.
Dropping a wrapper releases its grant.
Leaked wrappers retain their grants and storage holds, subject to quota and teardown.

Receive and completion records remain available until their grants are redeemed or explicitly discarded.
Each grant can construct only one local wrapper, even if polling repeats.
Cancellation requests do not return access; a return grant requires backend access to end.

### Keep device borrows until access ends {#device-borrows}

Kernelet drivers submit owning buffer wrappers through the trusted I/O interface.
Host adapters resolve buffer identities to retained storage and give backends only the access required by each operation.

| Operation | Backend access to buffer | Local access while accepted |
|---|---|---|
| Disk write or network transmit | Read | None |
| Disk read or network receive | Write | None |

[DMA directions](https://docs.kernel.org/6.12/core-api/dma-api-howto.html#dma-direction)

Before acceptance, the host validates a stable request snapshot and reserves operation and completion capacity.
It checks every range against the allocation and the caller's grant, including each scatter/gather segment.
The backend's access mode must be permitted by that grant.
The host installs the retained allocation and completion record before publishing work, because the backend can complete immediately.
Acceptance atomically ends the local grant and establishes the backend operation's storage hold.

```text
Local(A) -> Backend(op) -> Issued(A) -> Local(A)
             device          return grant
             uses X          for the same X
```

Completion validates the request identity and generation before changing trusted state.
The host returns access only after all backend accesses end and required DMA synchronization completes.
It checks the reported transfer length against the submitted range before recording the result.
If the submitter remains live, the host creates a return grant and vOSTD redeems it into a local wrapper.
Otherwise, completion releases the operation's storage hold without calling kernelet code or creating a return grant.
[DMA synchronization](https://docs.kernel.org/6.12/core-api/dma-api-howto.html#using-streaming-dma-mappings)

Acceptance, permission to reuse storage, and device-specific completion have distinct meanings.
The return grant establishes safe buffer reuse; the operation status reports the device-specific result.
Linux's [`MSG_ZEROCOPY` notifications](https://www.kernel.org/doc/html/v6.12/networking/msg_zerocopy.html#deferred-copies) illustrate the distinction:

> the kernel returns a completion notification when it releases its hold on the shared pages.

A notification can also follow a deferred copy, so it alone does not establish copy elimination.

### Hand off the original channel buffer {#channel-handoff}

The receiver advertises credit for imported buffer count and retained bytes.
The sender supplies the buffer, destination connection, requested mode, and payload length.
The host validates the connection, recipient lifetime, allocation, sender grant, range, and requested mode.
Handoff can preserve or reduce permissions; permission upgrades require a separate authorized operation.
The host reserves receiver credit and a delivery record before acceptance.

Acceptance atomically ends the sender's grant and issues a receiver grant for the same allocation.
The receiver redeems the queued grant into a local wrapper with the authorized permissions.
When finished, it releases the wrapper or forwards it through the same protocol.

```text
current lend-chan: sender buffer X -> payload copy -> receiver buffer Y
proposed handoff:  sender buffer X -> transfer access -> receiver uses X

Local(A) -> Issued(B) -> Local(B) -> release -> reclaim X
```

Successful handoff ends the sender's grant; send completion does not return buffer access.
To retain access during handoff, the sender must copy the payload or use a separate read-sharing protocol.
If B terminates before redemption, the host cancels B's issued grant and releases its reserved credit.
If A terminates after acceptance, B's grant remains valid and the host retains the allocation.
Receiver credit remains reserved until B releases its grant or an onward transfer replaces that reservation.

The transport API beneath the socket layer must accept and release imported buffers.
An interface that requires payloads in receiver-posted storage still needs a copy.
Ordinary frame handles and `VmSpace` mappings must not bypass the imported buffer's validated permissions.

### Handle rejection, cancellation, and teardown {#protocol-failures}

| Event | Required action |
|---|---|
| Validation or reservation fails before acceptance | Keep the original grant active and return the original wrapper exactly once |
| Backend rejects work after host acceptance | Finish the accepted operation with an error; return access only after confirming storage release |
| Timeout or cancellation request | Record the result or cancellation request; retain storage until backend access actually ends |
| Completion or receive queue is full | Preserve accepted state in host-owned records and apply backpressure; polling must recover pending results or grants |
| Duplicate or stale completion/release | Reject it without ending a newer grant or releasing a newer allocation |
| A local holder terminates | Stop its carriers and drain trusted local access before ending its active grants |
| Another holder or backend still uses the buffer | Preserve its grant or operation and the allocation's charge |
| Device access cannot be stopped | Retain storage and callback state until release can be established |

Normal release consumes a wrapper only after its accessor borrows end.
The existing [carrier-stop rules](../blueprint/asterinas-mode/faults-and-reclamation.md#stopping) state:

> No kernelet frame is resumed and no kernelet destructor runs.

Marking a sandbox dying prevents new buffer grants and operations for that sandbox.
The host uses the existing termination protocol to stop carriers after their trusted service calls finish or unwind.
Cleanup then ends the sandbox's active grants and cancels its unredeemed grants.
Cleanup uses host-owned records and does not depend on kernelet destructors.
[Asterinas termination](../blueprint/asterinas-mode/faults-and-reclamation.md#stopping), [Linux termination](../blueprint/linux-mode/faults-and-reclamation.md#safe)

Sender termination does not revoke grants issued to another live participant.
Accepted delivery records survive connection reset until redemption or explicit discard.
Backend operations and relevant callbacks retain their storage holds independently of local grants.
Cancellation or timeout cannot release storage while backend access remains possible.

The host reclaims a buffer only after all grant, backend, and relevant callback holds end.
It retains the allocation's pool slot, allocator metadata, and accounting record until reclamation.
These records must outlive the sender's image when another participant or backend still holds the buffer.
Quotas bound retained memory; reclamation time depends on backend release guarantees.

The [reclamation design](../blueprint/asterinas-mode/faults-and-reclamation.md) already permits a `Zombie` while lending remains outstanding.

## Implementation plan {#implementation-plan}

Implement the protocol in the following dependency order.
Each stage includes rejection, release, and quota handling for the states it introduces.

1. **Define the ABI and transition rules.**
   Define buffer, grant, and request identifiers, ranges, modes, and results in the shared C ABI.
   Specify atomic acceptance and rejection for each [transition](#protocol-states), with caller identity supplied by trusted service context.

2. **Implement the host pool and trusted records.**
   Add the shared buffer pool and [allocation, grant, and operation records](#buffer-record) in Host OSTD or the Linux host adapter.
   Initialize storage, enforce accounting, and retain each allocation and its allocator metadata until all holds end.

3. **Implement vOSTD wrappers and accessors.**
   Construct `ReadBuffer` and `ReadWriteBuffer` only after grant redemption, with the specified [accessor and consuming-operation rules](#access-transitions).
   Audit inherited methods and backing handles so safe callers cannot recover permissions excluded by the wrapper.

4. **Implement channel handoff and import.**
   Replace receiver-posted payload storage with bounded import credit and host-owned delivery records.
   Connect consuming send, grant redemption, and release beneath the socket layer so both endpoints use the original allocation.

5. **Adapt kernelet drivers and host device backends.**
   Make drivers submit owning wrappers and redeem return grants; make host adapters enforce the [device-access contract](#device-borrows).
   Integrate direct block I/O first, then network paths that can use these allocations directly.

6. **Integrate reset, teardown, and resource accounting.**
   Apply the existing carrier-stop protocol before ending local grants, while preserving receiver grants, backend holds, callback state, and retained charges.
   Make accepted work recoverable after notification failure and verify the [reclamation conditions](#protocol-failures).

The existing tenant-frame lending path remains available while backends gain shared-buffer support.
Use the copying transport when a peer cannot accept imports or a backend requires private payload storage.
Choose a copying fallback before acceptance, or wait until accepted work releases incompatible access before copying.

## Safety constraints and limitations {#safety}

Trusted vOSTD, host code, and physical drivers remain within the [trusted base](../blueprint/asterinas-mode/principles.md).
The protocol must maintain these invariants through every accepted transition:

- **Bounds and initialization:** expose only initialized bytes within the live allocation and the holder's authorized range.
- **Access permissions:** enforce single-use grants and local accessor lifetimes; read-only wrappers cannot create writers or recover writable backing handles.
- **Retention:** preserve the allocation and allocator metadata until all grant, backend, and relevant callback holds end.
- **Stable control information:** validate host-owned snapshots of request metadata and interpreted headers, following the existing [lending rules](../blueprint/asterinas-mode/zero-copy-io.md#lending).
- **Completion and termination:** reject stale identities, release each hold once, and end local grants only after local access has stopped.
- **Resource bounds:** reserve receiver credit and keep retained storage charged, including after sender termination.

Payload copy avoidance requires producers, consumers, and backends to use the same buffer storage.
Buffered file backends and shared network receive queues retain their copies unless their buffer interfaces also change.
DMA mapping may introduce [SWIOTLB bounce-buffer copies](https://docs.kernel.org/6.12/core-api/swiotlb.html).
Backend integration must satisfy device-specific mapping, synchronization, and calling-context requirements.

## Validation {#validation}

Verify range and permission enforcement, single-use grants, and allocation retention through completion and termination.
Compile-fail checks must reject unauthorized writers and moves while accessors remain borrowed.
Runtime checks must cover stale identities, delayed completion, cancellation, and termination without premature storage reuse or lost accounting.
Validate backend release and DMA synchronization on real devices.

Count payload copies across host–kernelet and inter-kernelet boundaries on complete I/O paths, including fallback and DMA bounce copies.
Compare CPU cost, latency, throughput, and retained memory against existing lending paths under the same backend, batching, interrupt policy, and workload.
Include small payloads and delayed receivers to measure tracking overhead and retention cost.

The existing [I/O microbenchmarks](io-microbenchmarks.md) measure unit costs; they do not validate this buffer contract or its end-to-end benefit.
**[unverified]** Performance gains depend on backend compatibility and whether tracking costs remain below the avoided copy cost.
