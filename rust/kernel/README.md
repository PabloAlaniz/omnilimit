# omnilimit-kernel

omnilimit's meter in a table of fixed size, for Rust. Not published yet: use it by path.

```rust
use omnilimit_kernel::{Limiter, Policy};

let limiter = Limiter::with_clients(Policy::new(5, 60_000, 5)?, 100_000);
let decision = limiter.check("203.0.113.7");
if !decision.allowed {
    // decision.retry_after_ms says how long to wait
}
```

- **Fixed memory.** 64 bytes for every 6 clients, decided when the limiter is created. There is no eviction and no cleanup thread: an entry whose meter has drained is a free slot.
- **It never admits more than the policy.** When a bucket is full, a new client is counted together with two others instead of pushing one out. That is stricter for those involved, never looser.
- **Lock-free for clients it knows.** A check reads one cache line and writes one word with a compare-and-swap. A denied check writes nothing.
- **The specification's numbers.** While a client has a slot of its own, `remaining`, `reset_ms` and `retry_after_ms` are the ones every omnilimit port computes.

It has no reset and no refund. What it promises exactly, how it is laid out and how it is checked are in [docs/kernel.md](../../docs/kernel.md).

## Tests

```sh
cargo test --release                                   # unit, properties, the spec's vectors
RUSTFLAGS="--cfg loom" LOOM_MAX_PREEMPTIONS=3 cargo test --release --test loom
MIRIFLAGS="-Zmiri-disable-isolation" cargo +nightly miri test --lib
```
