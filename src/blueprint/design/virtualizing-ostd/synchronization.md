# Synchronization

> **To be written.** Discuss the OSTD primitives below one family at a time, starting from their native implementation and then deciding what vOSTD needs to change.
> This page records the source inventory and open questions; the guard, waiting and RCU dependencies referenced by other chapters remain pending discussion.

## OSTD primitives {#primitives}

The current public exports in `ostd/src/sync/mod.rs` are:

```rust
pub use self::{
    guard::{GuardTransfer, LocalIrqDisabled, PreemptDisabled, SpinGuardian, WriteIrqDisabled},
    mutex::{Mutex, MutexGuard},
    rcu::{Rcu, RcuDrop, RcuOption, RcuOptionReadGuard, RcuReadGuard, non_null},
    rwarc::{RoArc, RwArc},
    rwlock::{RwLock, RwLockReadGuard, RwLockUpgradeableGuard, RwLockWriteGuard},
    rwmutex::{RwMutex, RwMutexReadGuard, RwMutexUpgradeableGuard, RwMutexWriteGuard},
    spin::{SpinLock, SpinLockGuard},
    wait::{WaitQueue, Waiter, Waker},
};
```

Atomic types from `core::sync::atomic` and initialization primitives from dependency crates are separate from these OSTD exports.

## Guards and guardians {#virtual-guards}

A lock guard, such as `MutexGuard`, represents acquired access to protected data and releases the lock when dropped.
The lower execution guards instead keep preemption or local interrupts disabled while they are held:

- `task::DisabledPreemptGuard`, returned by `task::disable_preempt`.
- `irq::DisabledLocalIrqGuard`, returned by `irq::disable_local`.

`SpinGuardian` selects the execution guards used by a spin-based lock:

- `PreemptDisabled`: disables preemption for both reading and writing.
- `LocalIrqDisabled`: disables local interrupts for both reading and writing.
- `WriteIrqDisabled`: disables preemption for reading and local interrupts for writing.

`GuardTransfer` transfers guard protection without a gap.
Related interfaces outside `sync` are `task::atomic_mode::{InAtomicMode, AsAtomicModeGuard, might_sleep}` and `cpu::PinCurrentCpu`.
They express or check the restrictions on sleeping and CPU migration while protection is held.
Sources: `ostd/src/sync/guard.rs`, `ostd/src/task/preempt/guard.rs`, `ostd/src/irq/guard.rs`, `ostd/src/task/atomic_mode.rs`, and `ostd/src/cpu/id.rs`.

> **To be written.** Explain nesting, release and guard transfer, then define what each guard protects in vOSTD: internal Task scheduling, virtual interrupt delivery and vCPU-local access.
> Distinguish those guarantees from Host preemption of the vCPU Thread.

## Spin locks {#spin-locks}

`SpinLock` provides exclusive access; `RwLock` provides shared reads, exclusive writes and an upgradeable read guard.
Their contention paths spin rather than putting the caller to sleep.
The guard types are `SpinLockGuard`, `RwLockReadGuard`, `RwLockWriteGuard` and `RwLockUpgradeableGuard`.
Sources: `ostd/src/sync/spin.rs` and `ostd/src/sync/rwlock.rs`.

> **To be written.** Trace acquisition and release with each guardian, including read/write conversions and access from interrupt callbacks.

## Sleeping locks {#sleeping-locks}

`Mutex` provides exclusive access; `RwMutex` provides shared reads, exclusive writes and upgradeable reads.
Their lock acquisition paths can wait through a `WaitQueue`.
The guard types are `MutexGuard`, `RwMutexReadGuard`, `RwMutexWriteGuard` and `RwMutexUpgradeableGuard`.
Sources: `ostd/src/sync/mutex.rs` and `ostd/src/sync/rwmutex.rs`.

> **To be written.** Follow contention, waiting, unlock and wakeup, then connect these operations to internal Task scheduling.
> Review upgrades separately: the current `RwMutexUpgradeableGuard::upgrade` spins while existing readers finish.

## WaitQueue, Waiter and Waker {#waiting}

`Waiter::new_pair` creates a waiter for the current Task and an associated `Arc<Waker>`.
`Waiter` waits; `Waker` records a wakeup and makes the associated Task runnable when needed.
`WaitQueue` organizes waiters around a condition and supports waking one or all of them.
Source: `ostd/src/sync/wait.rs`.

> **To be written.** Trace one complete wait/wake sequence, including a wakeup that arrives before sleeping.
> Identify which operations run in vOSTD and how they reach the internal scheduler.

## Waiting for an idle vCPU {#vcpu-wait}

> **To be written.** Separately discuss how Host OSTD uses its native Waiter/Waker to block and wake an idle vCPU Thread through `vcpu_wait` and `vcpu_notify`.
> Connect this to [Task idle](tasks.md#waiting), covering notification before sleeping and pending events without confusing this waiter with an internal Task's waiter.

## RCU {#rcu}

Read-copy update (RCU) lets readers continue using an old object after a replacement is published; reclamation waits for those readers to finish.
The public types are `Rcu`, `RcuOption`, `RcuReadGuard`, `RcuOptionReadGuard` and `RcuDrop`.
The `non_null` module supplies the `NonNullPtr` abstraction and its `BoxRef` and `ArcRef` helpers.
Source: `ostd/src/sync/rcu/`.

> **To be written.** Explain read protection, pointer replacement and delayed destruction, then review internal Task switches, idle vCPUs and readers paused by Host scheduling.
> Determine where grace-period completion is reported and which execution runs the callbacks.

## RwArc and RoArc {#rw-arc}

`RwArc` combines reference-counted ownership with read/write locking; `clone_ro` produces a `RoArc` with read-only access.
Source: `ostd/src/sync/rwarc.rs`.

> **To be written.** Explain shared ownership, read/write guards and the conditions for lockless access, then check whether their underlying lock adaptation is sufficient for vOSTD.

## Discussion and verification {#verification}

> **To be written.** For each family, distinguish unchanged native code from required vOSTD adaptations, show one concrete Rust call sequence, and identify the races that need verification.
> Detailed virtualization implementations will be added after that discussion.
