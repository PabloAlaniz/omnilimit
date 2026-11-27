//! A keyed rate limiter in fixed memory, lock-free for clients it already knows.
//!
//! It is omnilimit's algorithm, GCRA as a meter on integers, kept in a table that never grows.
//! One 64-byte bucket holds eight clients. A client is one 64-bit word: a 15-bit tag, a flag and
//! a 48-bit theoretical arrival time. A check for a known client reads one cache line and writes
//! one word with a compare-and-swap; a denied check writes nothing.
//!
//! ```
//! use omnilimit_kernel::{Limiter, Policy};
//!
//! let limiter = Limiter::with_clients(Policy::new(5, 60_000, 5)?, 100_000);
//! let decision = limiter.check("203.0.113.7");
//! assert!(decision.allowed);
//! assert_eq!(decision.remaining, 4);
//! # Ok::<(), omnilimit_kernel::PolicyError>(())
//! ```
//!
//! # What it promises
//!
//! * **It never admits more than the policy.** Whatever state a client reads is at least the
//!   state an exact limiter would hold for the requests this table admitted for it.
//! * **An expired entry is a free slot.** An arrival time in the past carries no information, so
//!   there is no eviction and no housekeeping thread: room appears as time passes.
//! * **A full bucket shares, it does not forget.** When eight live clients already sit in a
//!   bucket, a newcomer is counted in two of its slots, chosen by its hash, next to their owners:
//!   it reads the smaller of the two and raises both. That is a count-min sketch inside the
//!   bucket. It is stricter for those involved and never looser.
//!
//! While a client has a slot of its own and the clock does not go back, every number in a
//! [`Decision`] is the one omnilimit's specification computes.
//!
//! # A clock that goes back
//!
//! The limiter keeps one number per client where the specification keeps two, so it cannot tell
//! how far back a clock went. Two things follow.
//!
//! A request that arrives with an earlier time finds every meter as full as it was then, or
//! fuller, and is denied where the specification would count on from the later time. That is
//! stricter.
//!
//! A meter that had drained by a time the limiter has seen is forgotten, and its slot may go to
//! someone else. A later request with an earlier time then starts from an empty meter, as it does
//! in any store that expires its buckets. So the first promise, put exactly: it never admits more
//! than the specification does in a store that forgets a meter once it has drained.
//!
//! # What it does not do
//!
//! There is no reset and no refund: a client that shares a slot cannot be taken out of it. Two
//! clients whose hashes agree on bucket and tag are limited as one: in a full bucket, a client
//! shares its tag with another about once in 4,700.

use std::hash::{BuildHasher, Hash};
use std::sync::atomic::Ordering::{Acquire, Relaxed, Release};
use std::time::Instant;

mod sync {
    #[cfg(loom)]
    pub use loom::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize};
    #[cfg(not(loom))]
    pub use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize};

    /// Wait a moment for a lock: spin briefly, then give the processor away.
    #[inline(always)]
    pub fn pause(_spins: &mut u32) {
        #[cfg(loom)]
        loom::thread::yield_now();
        #[cfg(not(loom))]
        {
            *_spins += 1;
            if *_spins < 64 {
                std::hint::spin_loop()
            } else {
                std::thread::yield_now()
            }
        }
    }
}
use sync::{AtomicBool, AtomicU64, AtomicUsize};

/// Clients per bucket. The model checker runs with two, which is enough for every path.
#[cfg(not(loom))]
pub const SLOTS: usize = 8;
#[cfg(loom)]
pub const SLOTS: usize = 2;
const ALL: u32 = (1 << SLOTS) - 1;
const TAT_MASK: u64 = (1 << 48) - 1;
const SHARED: u64 = 1 << 48; // the slot also counts clients that found no room
const TAG_SHIFT: u32 = 49;
/// The clock is 48 bits wide and wraps. An arrival time up to half a turn ahead of `now` is in
/// the future, and the other half is the past: a thread whose clock is a little behind another's
/// still sees a full meter as full, not as expired.
const AHEAD: u64 = 1 << 47;
/// A burst is at most an eighth of a turn, far inside the half that counts as the future.
const MAX_TAU: u64 = 1 << 45;
/// At most 2^27 ticks per millisecond, so a turn of the clock lasts 35 minutes or more.
const MAX_RATE: u64 = 1 << 27;
const SWEEP_SHIFT: u32 = 36; // one sweep step every 2^36 ticks
const SWEEP_STEPS: u64 = 1024; // a full pass every 2^46 ticks: nothing expired lives to look ahead again

#[cfg_attr(not(loom), repr(C, align(64)))]
struct Bucket([AtomicU64; SLOTS]);
#[cfg(not(loom))]
const _: () = assert!(std::mem::size_of::<Bucket>() == 64);

/// Taken only by a client that has no slot yet. Known clients never touch it.
#[cfg_attr(not(loom), repr(align(64)))]
struct Stripe(AtomicBool);

/// Why a policy was refused. The rules are those of omnilimit's specification.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PolicyError {
    /// `limit`, `window_ms` and `burst` must all be at least 1.
    Zero,
    /// `window_ms` must be at least 1000.
    WindowTooShort,
    /// `2 * burst * window_ms + limit` must not pass `2^53 - 1`.
    TooLarge,
    /// A shorthand must look like `10/minute` or `5/15minute`.
    Shorthand,
}
impl std::fmt::Display for PolicyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            PolicyError::Zero => "limit, window_ms and burst must be positive integers",
            PolicyError::WindowTooShort => "window_ms must be at least 1000",
            PolicyError::TooLarge => "policy too large for exact integer arithmetic (2*burst*window_ms + limit)",
            PolicyError::Shorthand => "a policy shorthand looks like \"10/minute\" or \"5/15minute\" (second, minute, hour, day)",
        })
    }
}
impl std::error::Error for PolicyError {}

/// A cost that is not an integer from 1 to the burst: it could never be allowed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CostError {
    pub cost: u64,
    pub burst: u64,
}
impl std::fmt::Display for CostError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "cost must be an integer from 1 to the burst ({}), got {}", self.burst, self.cost)
    }
}
impl std::error::Error for CostError {}

/// `limit` requests per `window_ms`, with up to `burst` at once.
///
/// A tick of the limiter's clock is `1/limit` of a millisecond, so `now_ms * limit` is an
/// integer and a request costs `window_ms` ticks: the meter of the specification, exactly.
///
/// Only when `burst * window_ms` reaches `2^45`, or `limit` passes `2^27`, is the clock made
/// coarser, by a power of two, and the cost of a request rounded up to it. The limiter is then
/// stricter than the policy, never looser, and [`Policy::is_exact`] says so.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Policy {
    limit: u64,
    window_ms: u64,
    burst: u64,
    shift: u32,    // ticks are 2^shift / limit milliseconds
    interval: u64, // ticks one request costs
    tau: u64,      // ticks of burst: burst * interval
}

impl Policy {
    pub fn new(limit: u64, window_ms: u64, burst: u64) -> Result<Self, PolicyError> {
        if limit == 0 || window_ms == 0 || burst == 0 {
            return Err(PolicyError::Zero);
        }
        if window_ms < 1000 {
            return Err(PolicyError::WindowTooShort);
        }
        // 2 * burst * window_ms + limit <= 2^53 - 1, written so that nothing overflows.
        let max = (1u128 << 53) - 1;
        if limit as u128 > max || burst as u128 * window_ms as u128 > (max - limit as u128) / 2 {
            return Err(PolicyError::TooLarge);
        }
        let mut shift = 0u32;
        let interval = loop {
            let interval = (window_ms + (1 << shift) - 1) >> shift;
            if burst as u128 * interval as u128 <= MAX_TAU as u128 && ((limit - 1) >> shift) < MAX_RATE {
                break interval;
            }
            shift += 1;
        };
        Ok(Policy { limit, window_ms, burst, shift, interval, tau: burst * interval })
    }

    /// `limit` per `window_ms`, with a burst equal to the limit.
    pub fn per(limit: u64, window_ms: u64) -> Result<Self, PolicyError> {
        Self::new(limit, window_ms, limit)
    }

    pub fn limit(&self) -> u64 {
        self.limit
    }
    pub fn window_ms(&self) -> u64 {
        self.window_ms
    }
    pub fn burst(&self) -> u64 {
        self.burst
    }
    /// True when the limiter's numbers are those of the specification, which is all but the very
    /// largest policies.
    pub fn is_exact(&self) -> bool {
        self.shift == 0
    }

    #[inline(always)]
    fn time(&self, now_ms: u64) -> Time {
        let ticks = (now_ms as u128 * self.limit as u128) >> self.shift;
        Time { now: ticks as u64 & TAT_MASK, epoch: (ticks >> SWEEP_SHIFT) as u64 }
    }
    #[inline(always)]
    fn ms(&self, ticks: u64) -> u64 {
        ((ticks as u128) << self.shift).div_ceil(self.limit as u128) as u64
    }
}

/// A policy written as `<limit>/<count><unit>`: `10/minute`, `5/15minute`, `1000/day`. The
/// grammar is the specification's, and strict on purpose: no spaces, no plurals, no upper case.
impl std::str::FromStr for Policy {
    type Err = PolicyError;
    fn from_str(text: &str) -> Result<Self, PolicyError> {
        fn number(digits: &str) -> Option<u64> {
            let ok = (1..=15).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit()) && !digits.starts_with('0');
            if ok {
                digits.parse().ok()
            } else {
                None
            }
        }
        let (limit, rest) = text.split_once('/').ok_or(PolicyError::Shorthand)?;
        let split = rest.find(|c: char| !c.is_ascii_digit()).ok_or(PolicyError::Shorthand)?;
        let (count, unit) = rest.split_at(split);
        let unit_ms = match unit {
            "second" => 1000,
            "minute" => 60_000,
            "hour" => 3_600_000,
            "day" => 86_400_000,
            _ => return Err(PolicyError::Shorthand),
        };
        let limit = number(limit).ok_or(PolicyError::Shorthand)?;
        let count = if count.is_empty() { 1 } else { number(count).ok_or(PolicyError::Shorthand)? };
        Policy::per(limit, count.checked_mul(unit_ms).ok_or(PolicyError::TooLarge)?)
    }
}

/// The clock in ticks, modulo `2^48`, and the same clock in sweep steps, not wrapped.
#[derive(Clone, Copy)]
struct Time {
    now: u64,
    epoch: u64,
}

/// The answer to one check, with the public numbers of the specification.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Decision {
    pub allowed: bool,
    /// Requests of cost 1 that would still be allowed right now.
    pub remaining: u64,
    /// Milliseconds until the meter is empty.
    pub reset_ms: u64,
    /// When denied, the milliseconds to wait before the same request would be allowed.
    pub retry_after_ms: Option<u64>,
}

/// A keyed rate limiter for one policy, in a table of fixed size.
pub struct Limiter<S = ahash::RandomState> {
    buckets: Box<[Bucket]>,
    stripes: Box<[Stripe]>,
    nb: u64,
    policy: Policy,
    hasher: S,
    epoch: AtomicU64,
    cursor: AtomicUsize,
    started: Instant,
}

/// One lock per 64 buckets, up to 1024: 1.5% of the table at most.
fn stripes_for(nb: usize) -> usize {
    (nb / 64).clamp(1, 1024).next_power_of_two()
}

#[cfg(not(loom))]
fn table<T>(n: usize) -> Box<[T]> {
    use std::alloc::{alloc_zeroed, handle_alloc_error, Layout};
    let layout = Layout::array::<T>(n).expect("table too large");
    // Only called for `Bucket` and `Stripe`: atomics of integers and booleans, for which all
    // zeroes is a valid value, and the empty one. `n` is never 0.
    unsafe {
        let p = alloc_zeroed(layout) as *mut T;
        if p.is_null() {
            handle_alloc_error(layout)
        }
        Box::from_raw(std::ptr::slice_from_raw_parts_mut(p, n))
    }
}

impl Limiter<ahash::RandomState> {
    /// A limiter for about `clients` clients active at once. It takes 64 bytes for every 6 of
    /// them. More clients than that are still limited, some of them together: see the crate docs.
    pub fn with_clients(policy: Policy, clients: usize) -> Self {
        Self::with_buckets(policy, clients.div_ceil(6).max(1))
    }

    /// A limiter in `bytes` of memory, locks included, rounded down to whole cache lines.
    pub fn with_memory(policy: Policy, bytes: usize) -> Self {
        let lines = (bytes / 64).max(2);
        Self::with_buckets(policy, lines - stripes_for(lines).min(lines - 1))
    }

    /// A limiter of exactly `buckets` buckets of [`SLOTS`] clients.
    pub fn with_buckets(policy: Policy, buckets: usize) -> Self {
        // Random per process: a client cannot choose ids that land in one bucket.
        Self::with_buckets_and_hasher(policy, buckets, ahash::RandomState::new())
    }
}

impl<S: BuildHasher> Limiter<S> {
    pub fn with_buckets_and_hasher(policy: Policy, buckets: usize, hasher: S) -> Self {
        let nb = buckets.max(1);
        #[cfg(not(loom))]
        let (buckets, stripes) = (table::<Bucket>(nb), table::<Stripe>(stripes_for(nb)));
        #[cfg(loom)]
        let (buckets, stripes) = (
            (0..nb).map(|_| Bucket(std::array::from_fn(|_| AtomicU64::new(0)))).collect::<Box<[_]>>(),
            (0..stripes_for(nb)).map(|_| Stripe(AtomicBool::new(false))).collect::<Box<[_]>>(),
        );
        Limiter {
            buckets,
            stripes,
            nb: nb as u64,
            policy,
            hasher,
            epoch: AtomicU64::new(u64::MAX),
            cursor: AtomicUsize::new(0),
            started: Instant::now(),
        }
    }

    pub fn policy(&self) -> &Policy {
        &self.policy
    }
    /// Bytes of the table and of its locks.
    pub fn memory_bytes(&self) -> usize {
        (self.buckets.len() + self.stripes.len()) * 64
    }

    /// Milliseconds on the limiter's own clock, which starts when it is created and never goes back.
    pub fn now_ms(&self) -> u64 {
        self.started.elapsed().as_millis() as u64 + 1
    }

    /// The hash the limiter uses for `key`, for callers that check the same key more than once.
    #[inline(always)]
    pub fn hash<K: Hash + ?Sized>(&self, key: &K) -> u64 {
        self.hasher.hash_one(key)
    }

    /// Count one request for `key`, now.
    pub fn check<K: Hash + ?Sized>(&self, key: &K) -> Decision {
        self.check_at(key, self.now_ms())
    }

    /// Count one request for `key` at `now_ms`, a clock of the caller's in milliseconds.
    pub fn check_at<K: Hash + ?Sized>(&self, key: &K, now_ms: u64) -> Decision {
        let (allowed, level) = self.apply(self.hash(key), self.policy.time(now_ms), self.policy.interval, true);
        self.decision(allowed, level, self.policy.interval)
    }

    /// Count a request that weighs `cost` units, such as a batch of that many items. A denied
    /// request counts nothing, whatever its cost.
    pub fn check_cost_at<K: Hash + ?Sized>(&self, key: &K, cost: u64, now_ms: u64) -> Result<Decision, CostError> {
        let add = self.cost(cost)?;
        let (allowed, level) = self.apply(self.hash(key), self.policy.time(now_ms), add, true);
        Ok(self.decision(allowed, level, add))
    }

    /// What [`Limiter::check_at`] would answer, without counting anything.
    pub fn peek_at<K: Hash + ?Sized>(&self, key: &K, now_ms: u64) -> Decision {
        let (allowed, level) = self.apply(self.hash(key), self.policy.time(now_ms), self.policy.interval, false);
        self.decision(allowed, level, self.policy.interval)
    }

    /// What [`Limiter::check_cost_at`] would answer, without counting anything.
    pub fn peek_cost_at<K: Hash + ?Sized>(&self, key: &K, cost: u64, now_ms: u64) -> Result<Decision, CostError> {
        let add = self.cost(cost)?;
        let (allowed, level) = self.apply(self.hash(key), self.policy.time(now_ms), add, false);
        Ok(self.decision(allowed, level, add))
    }

    /// Count one request and say only whether it is allowed: the fastest call there is. `hash`
    /// comes from [`Limiter::hash`].
    #[inline(always)]
    pub fn allow_hashed_at(&self, hash: u64, now_ms: u64) -> bool {
        self.apply(hash, self.policy.time(now_ms), self.policy.interval, true).0
    }

    /// Count one request for `key` at `now_ms` and say only whether it is allowed.
    #[inline(always)]
    pub fn allow_at<K: Hash + ?Sized>(&self, key: &K, now_ms: u64) -> bool {
        self.allow_hashed_at(self.hash(key), now_ms)
    }

    fn cost(&self, cost: u64) -> Result<u64, CostError> {
        if cost == 0 || cost > self.policy.burst {
            return Err(CostError { cost, burst: self.policy.burst });
        }
        Ok(cost * self.policy.interval)
    }

    fn decision(&self, allowed: bool, level: u64, add: u64) -> Decision {
        let p = &self.policy;
        Decision {
            allowed,
            // A meter reads above its capacity only to a clock that went back.
            remaining: (p.tau - level.min(p.tau)) / p.interval,
            reset_ms: p.ms(level),
            retry_after_ms: if allowed { None } else { Some(p.ms(level + add - p.tau)) },
        }
    }

    /// Bucket, 15-bit tag (never 0, so a used word is never 0), and spare hash bits that pick the
    /// two slots to share when there is no room.
    #[inline(always)]
    fn place(&self, h: u64) -> (usize, u64, u32) {
        let tag = h & 0x7FFF;
        ((((h >> 16) as u128 * self.nb as u128) >> 48) as usize, tag + (tag == 0) as u64, (h >> 15) as u32)
    }

    /// Whether the request is allowed, and the level of the meter in ticks: after the request
    /// when it was counted, before it otherwise.
    #[inline(always)]
    fn apply(&self, h: u64, t: Time, add: u64, write: bool) -> (bool, u64) {
        if t.epoch != self.epoch.load(Relaxed) {
            self.sweep(t);
        }
        let (b, tag, aux) = self.place(h);
        let slots = &self.buckets[b].0;
        let tau = self.policy.tau;
        loop {
            let (w, eq) = find(slots, tag, t.now);
            if eq == 0 {
                return self.apply_absent(b, tag, aux, t.now, add, write);
            }
            // A known client: one word, one compare-and-swap. A denial writes nothing.
            let i = eq.trailing_zeros() as usize;
            let level = w[i].wrapping_sub(t.now) & TAT_MASK;
            let next = level + add;
            if next > tau {
                return (false, level);
            }
            if !write {
                return (true, level);
            }
            let word = (w[i] & !TAT_MASK) | (t.now.wrapping_add(next) & TAT_MASK);
            if slots[i].compare_exchange(w[i], word, Relaxed, Relaxed).is_ok() {
                return (true, next);
            }
        }
    }

    /// The client has no live slot. What it may have been counted under is the two shared slots
    /// its hash picks: it reads the smaller. A slot that is not shared and live never held it, or
    /// expired since, and a state that expired is an empty one.
    ///
    /// New clients of a bucket take turns, so the same client cannot be given two slots. Clients
    /// that already have one keep going without waiting, which is why every write here is still a
    /// compare-and-swap.
    #[inline(never)]
    fn apply_absent(&self, b: usize, tag: u64, aux: u32, now: u64, add: u64, write: bool) -> (bool, u64) {
        let slots = &self.buckets[b].0;
        let tau = self.policy.tau;
        let (c1, c2) = candidates(aux);
        let shared = |w: &[u64; SLOTS], i: usize| if w[i] & SHARED != 0 && live(w[i], now) { w[i].wrapping_sub(now) & TAT_MASK } else { 0 };
        if !write {
            let w = read(slots);
            let level = shared(&w, c1).min(shared(&w, c2));
            return (level + add <= tau, level);
        }
        let stripe = &self.stripes[b & (self.stripes.len() - 1)].0;
        let mut spins = 0u32;
        while stripe.load(Relaxed) || stripe.compare_exchange_weak(false, true, Acquire, Relaxed).is_err() {
            sync::pause(&mut spins);
        }
        let out = 'decide: loop {
            let (w, eq) = find(slots, tag, now);
            if eq != 0 {
                // It arrived while this thread waited.
                let i = eq.trailing_zeros() as usize;
                let level = w[i].wrapping_sub(now) & TAT_MASK;
                if level + add > tau {
                    break (false, level);
                }
                let word = (w[i] & !TAT_MASK) | (now.wrapping_add(level + add) & TAT_MASK);
                if slots[i].compare_exchange(w[i], word, Relaxed, Relaxed).is_ok() {
                    break (true, level + add);
                }
                continue;
            }
            let level = shared(&w, c1).min(shared(&w, c2));
            let next = level + add;
            if next > tau {
                break (false, level);
            }
            let free = !live_in(&w, now) & ALL;
            if free != 0 {
                // Its own expired slot first: a thread whose clock is a moment behind may still be writing to it.
                let own = (0..SLOTS).find(|&j| free >> j & 1 != 0 && w[j] >> TAG_SHIFT == tag);
                let i = own.unwrap_or(free.trailing_zeros() as usize);
                let word = tag << TAG_SHIFT | (now.wrapping_add(next) & TAT_MASK);
                if slots[i].compare_exchange(w[i], word, Relaxed, Relaxed).is_ok() {
                    break (true, next);
                }
                continue;
            }
            // Every slot holds a live client: count this one in both of its slots, next to their owners.
            for i in [c1, c2] {
                loop {
                    let x = slots[i].load(Relaxed);
                    if !live(x, now) {
                        continue 'decide;
                    } // it expired meanwhile: there is room now
                    let level = (x.wrapping_sub(now) & TAT_MASK).max(next);
                    let word = (x & !TAT_MASK) | SHARED | (now.wrapping_add(level) & TAT_MASK);
                    if slots[i].compare_exchange(x, word, Relaxed, Relaxed).is_ok() {
                        break;
                    }
                }
            }
            break (true, next);
        };
        stripe.store(false, Release);
        out
    }

    /// An entry that expired half a turn of the clock ago would look ahead of `now` again. A slice
    /// of the table is cleaned every `2^36` ticks, a full pass every `2^46`, and nothing expired
    /// stays long enough for that.
    #[cold]
    #[inline(never)]
    fn sweep(&self, t: Time) {
        let old = self.epoch.load(Relaxed);
        if old != u64::MAX && t.epoch <= old {
            return;
        } // the clock went back, or another thread got here first
        if self.epoch.compare_exchange(old, t.epoch, Relaxed, Relaxed).is_err() || old == u64::MAX {
            return;
        }
        let steps = (t.epoch - old).min(SWEEP_STEPS);
        // After a silence of a full pass or more, everything written before it has expired, though
        // some of it may read as ahead by now. What other threads are writing this instant is at
        // most a burst ahead: a quarter turn tells the two apart.
        let ahead = if steps == SWEEP_STEPS { AHEAD / 2 } else { AHEAD };
        let n = self.buckets.len();
        let chunk = ((n as u128 * steps as u128).div_ceil(SWEEP_STEPS as u128) as usize).min(n);
        let start = self.cursor.fetch_add(chunk, Relaxed);
        for i in 0..chunk {
            for slot in &self.buckets[start.wrapping_add(i) % n].0 {
                let x = slot.load(Relaxed);
                if x != 0 && (x.wrapping_sub(t.now).wrapping_sub(1) & TAT_MASK) >= ahead {
                    let _ = slot.compare_exchange(x, 0, Relaxed, Relaxed);
                }
            }
        }
    }

    /// Slots that hold one client, slots shared by several, and free slots, at `now_ms`. It walks
    /// the whole table: for sizing and for tests.
    pub fn census(&self, now_ms: u64) -> Census {
        let now = self.policy.time(now_ms).now;
        let mut c = Census::default();
        for bucket in self.buckets.iter() {
            for slot in &bucket.0 {
                let x = slot.load(Relaxed);
                if !live(x, now) {
                    c.free += 1
                } else if x & SHARED != 0 {
                    c.shared += 1
                } else {
                    c.owned += 1
                }
            }
        }
        c
    }
}

/// For a limiter owned by one thread: the same decisions with no lock and no compare-and-swap.
#[cfg(not(loom))]
impl<S: BuildHasher> Limiter<S> {
    /// [`Limiter::allow_hashed_at`] through `&mut self`.
    #[inline(always)]
    pub fn allow_hashed_at_mut(&mut self, hash: u64, now_ms: u64) -> bool {
        let (t, add) = (self.policy.time(now_ms), self.policy.interval);
        self.apply_mut(hash, t, add, true).0
    }

    /// [`Limiter::check_at`] through `&mut self`.
    pub fn check_at_mut<K: Hash + ?Sized>(&mut self, key: &K, now_ms: u64) -> Decision {
        let (h, t, add) = (self.hash(key), self.policy.time(now_ms), self.policy.interval);
        let (allowed, level) = self.apply_mut(h, t, add, true);
        self.decision(allowed, level, add)
    }

    #[inline(always)]
    fn apply_mut(&mut self, h: u64, t: Time, add: u64, write: bool) -> (bool, u64) {
        if t.epoch != *self.epoch.get_mut() {
            self.sweep(t);
        }
        let (b, tag, aux) = self.place(h);
        let (now, tau) = (t.now, self.policy.tau);
        // Exclusive access: the eight words are plain memory here. An `AtomicU64` has the layout of a `u64`.
        let w: &mut [u64; SLOTS] = unsafe { &mut *(&mut self.buckets[b] as *mut Bucket as *mut [u64; SLOTS]) };
        let eq = find_in(w, tag, now);
        if eq != 0 {
            let i = eq.trailing_zeros() as usize;
            let level = w[i].wrapping_sub(now) & TAT_MASK;
            if level + add > tau {
                return (false, level);
            }
            if write {
                w[i] = (w[i] & !TAT_MASK) | (now.wrapping_add(level + add) & TAT_MASK);
            }
            return (true, if write { level + add } else { level });
        }
        let (c1, c2) = candidates(aux);
        let shared = |w: &[u64; SLOTS], i: usize| if w[i] & SHARED != 0 && live(w[i], now) { w[i].wrapping_sub(now) & TAT_MASK } else { 0 };
        let level = shared(w, c1).min(shared(w, c2));
        let next = level + add;
        if next > tau {
            return (false, level);
        }
        if !write {
            return (true, level);
        }
        let free = !live_in(w, now) & ALL;
        if free != 0 {
            w[free.trailing_zeros() as usize] = tag << TAG_SHIFT | (now.wrapping_add(next) & TAT_MASK);
            return (true, next);
        }
        for i in [c1, c2] {
            let level = (w[i].wrapping_sub(now) & TAT_MASK).max(next);
            w[i] = (w[i] & !TAT_MASK) | SHARED | (now.wrapping_add(level) & TAT_MASK);
        }
        (true, next)
    }
}

#[derive(Default, Debug, Clone, Copy, PartialEq, Eq)]
pub struct Census {
    pub owned: u64,
    pub shared: u64,
    pub free: u64,
}

/// The two slots a client is counted in when its bucket has no room: different, and its own.
#[inline(always)]
fn candidates(aux: u32) -> (usize, usize) {
    let c1 = (((aux & 0xFFF) * SLOTS as u32) >> 12) as usize;
    (c1, (c1 + 1 + ((((aux >> 12) & 0xFFF) * (SLOTS as u32 - 1)) >> 12) as usize) % SLOTS)
}

/// A word holds a client whose arrival time is ahead of `now`, on a clock that wraps at `2^48`.
#[inline(always)]
fn live(x: u64, now: u64) -> bool {
    x != 0 && (x.wrapping_sub(now).wrapping_sub(1) & TAT_MASK) < AHEAD
}

#[inline(always)]
fn read(slots: &[AtomicU64; SLOTS]) -> [u64; SLOTS] {
    let mut w = [0u64; SLOTS];
    for i in 0..SLOTS {
        w[i] = slots[i].load(Relaxed);
    }
    w
}

/// The words as read, each atomically, and one bit per slot that holds this tag and is live.
#[inline(always)]
fn find(slots: &[AtomicU64; SLOTS], tag: u64, now: u64) -> ([u64; SLOTS], u32) {
    let w = read(slots);
    let eq = find_in(&w, tag, now);
    (w, eq)
}

#[cfg(all(target_arch = "aarch64", not(loom), not(miri)))]
mod simd {
    use super::{AHEAD, SLOTS, TAG_SHIFT};
    use std::arch::aarch64::*;
    const WEIGHTS: [u64; 8] = [1, 2, 4, 8, 16, 32, 64, 128];
    const SHIFT: i32 = TAG_SHIFT as i32;

    /// Ahead of `now`: bit 47 of `tat - now - 1` is clear. The bits above it belong to the tag.
    #[inline(always)]
    unsafe fn ahead(x: uint64x2_t, now1: uint64x2_t) -> uint64x2_t {
        vceqzq_u64(vandq_u64(vsubq_u64(x, now1), vdupq_n_u64(AHEAD)))
    }

    #[inline(always)]
    pub fn find_in(w: &[u64; SLOTS], tag: u64, now: u64) -> u32 {
        // Safety: NEON is part of every aarch64 target, and each load reads two of the eight words.
        unsafe {
            let (now1, tagv) = (vdupq_n_u64(now.wrapping_add(1)), vdupq_n_u64(tag));
            let mut eq = vdupq_n_u64(0);
            for i in 0..4 {
                let x = vld1q_u64(w.as_ptr().add(2 * i));
                // A tag is never 0, so an empty word never matches.
                let hit = vandq_u64(ahead(x, now1), vceqq_u64(vshrq_n_u64::<SHIFT>(x), tagv));
                eq = vorrq_u64(eq, vandq_u64(hit, vld1q_u64(WEIGHTS.as_ptr().add(2 * i))));
            }
            (vgetq_lane_u64::<0>(eq) | vgetq_lane_u64::<1>(eq)) as u32
        }
    }

    #[inline(always)]
    pub fn live_in(w: &[u64; SLOTS], now: u64) -> u32 {
        // Safety: as above.
        unsafe {
            let now1 = vdupq_n_u64(now.wrapping_add(1));
            let mut live = vdupq_n_u64(0);
            for i in 0..4 {
                let x = vld1q_u64(w.as_ptr().add(2 * i));
                let l = vandq_u64(vtstq_u64(x, x), ahead(x, now1));
                live = vorrq_u64(live, vandq_u64(l, vld1q_u64(WEIGHTS.as_ptr().add(2 * i))));
            }
            (vgetq_lane_u64::<0>(live) | vgetq_lane_u64::<1>(live)) as u32
        }
    }
}
#[cfg(not(all(target_arch = "aarch64", not(loom), not(miri))))]
use scalar::{find_in, live_in};
#[cfg(all(target_arch = "aarch64", not(loom), not(miri)))]
use simd::{find_in, live_in};

#[allow(dead_code)]
mod scalar {
    use super::{live, SLOTS, TAG_SHIFT};

    /// One bit per slot that holds this tag and is live. A tag is never 0, so an empty word never matches.
    #[inline(always)]
    pub fn find_in(w: &[u64; SLOTS], tag: u64, now: u64) -> u32 {
        let mut eq = 0u32;
        for (i, &x) in w.iter().enumerate() {
            eq |= ((x >> TAG_SHIFT == tag && live(x, now)) as u32) << i;
        }
        eq
    }

    /// One bit per slot whose client is still live.
    #[inline(always)]
    pub fn live_in(w: &[u64; SLOTS], now: u64) -> u32 {
        let mut mask = 0u32;
        for (i, &x) in w.iter().enumerate() {
            mask |= (live(x, now) as u32) << i;
        }
        mask
    }
}

#[cfg(all(test, not(loom)))]
mod tests;
