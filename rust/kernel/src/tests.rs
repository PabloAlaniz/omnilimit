use super::*;
use std::collections::HashMap;

/// The meter of the specification, written from its text and nothing else.
#[derive(Clone, Copy, Default)]
pub(crate) struct Meter {
    level: u64,
    ts: u64,
}
pub(crate) struct Spec {
    limit: u64,
    window_ms: u64,
    capacity: u64,
    drain: u64,
}
impl Spec {
    pub(crate) fn new(limit: u64, window_ms: u64, burst: u64) -> Self {
        let capacity = burst * window_ms;
        Spec { limit, window_ms, capacity, drain: capacity.div_ceil(limit) }
    }
    fn drained(&self, m: Option<Meter>, now: u64) -> (u64, u64) {
        let m = m.unwrap_or(Meter { level: 0, ts: now });
        let t = now.max(m.ts);
        (m.level.saturating_sub((t - m.ts).min(self.drain) * self.limit), t)
    }
    fn decision(&self, allowed: bool, level: u64, add: u64) -> Decision {
        Decision {
            allowed,
            remaining: (self.capacity - level) / self.window_ms,
            reset_ms: level.div_ceil(self.limit),
            retry_after_ms: if allowed { None } else { Some((level + add - self.capacity).div_ceil(self.limit)) },
        }
    }
    pub(crate) fn hit(&self, m: &mut Option<Meter>, cost: u64, now: u64) -> Decision {
        let (lvl, t) = self.drained(*m, now);
        let add = cost * self.window_ms;
        if lvl + add > self.capacity {
            return self.decision(false, lvl, add);
        }
        *m = Some(Meter { level: lvl + add, ts: t });
        self.decision(true, lvl + add, add)
    }
    pub(crate) fn peek(&self, m: Option<Meter>, cost: u64, now: u64) -> Decision {
        let (lvl, _) = self.drained(m, now);
        let add = cost * self.window_ms;
        self.decision(lvl + add <= self.capacity, lvl, add)
    }
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: u64) -> u64 {
        ((self.next() as u128 * n as u128) >> 64) as u64
    }
}
/// Fewer rounds under the interpreter, which is hundreds of times slower.
fn scale(n: u64) -> u64 {
    if cfg!(miri) {
        (n / 2000).max(20)
    } else {
        n
    }
}
const T0: u64 = 1_700_000_000_000;

#[test]
fn burst_then_rate_for_one_client() {
    let limiter = Limiter::with_buckets(Policy::new(5, 60_000, 5).unwrap(), 64);
    for left in (0..5).rev() {
        let d = limiter.check_at("a", T0);
        assert!(d.allowed);
        assert_eq!(d.remaining, left);
    }
    assert_eq!(limiter.check_at("a", T0), Decision { allowed: false, remaining: 0, reset_ms: 60_000, retry_after_ms: Some(12_000) });
    assert!(!limiter.check_at("a", T0 + 11_999).allowed);
    assert!(limiter.check_at("a", T0 + 12_000).allowed);
    assert!(!limiter.check_at("a", T0 + 12_001).allowed);
    assert!(limiter.check_at("b", T0).allowed);
}

#[test]
fn the_own_clock_counts_too() {
    let limiter = Limiter::with_clients(Policy::new(2, 60_000, 2).unwrap(), 10);
    assert!(limiter.check("a").allowed);
    assert!(limiter.check("a").allowed);
    assert!(!limiter.check("a").allowed);
}

/// Roomy table, hits and peeks of any cost, a clock that only goes forward: every number equals
/// the specification's, through the shared path and through the exclusive one.
#[test]
fn every_number_is_the_specifications_while_there_is_room() {
    for (limit, window_ms, burst) in [(20, 10_000, 20), (3, 1_000, 7), (1000, 60_000, 50), (7, 86_400_000, 3)] {
        for exclusive in [false, true] {
            let spec = Spec::new(limit, window_ms, burst);
            let keys = 300u64;
            let mut limiter = Limiter::with_buckets(Policy::new(limit, window_ms, burst).unwrap(), 4096);
            let mut meters: HashMap<u64, Option<Meter>> = HashMap::new();
            let mut r = Rng(limit);
            let mut now = T0;
            let (mut same, mut total) = (0u64, 0u64);
            for _ in 0..scale(60_000) {
                now += r.below(1 + 2 * window_ms / limit / 3);
                let k = r.below(keys);
                let cost = if r.below(4) == 0 { 1 + r.below(burst) } else { 1 };
                let meter = meters.entry(k).or_default();
                let (want, got) = if r.below(5) == 0 {
                    (spec.peek(*meter, cost, now), limiter.peek_cost_at(&k, cost, now).unwrap())
                } else if exclusive && cost == 1 {
                    (spec.hit(meter, 1, now), limiter.check_at_mut(&k, now))
                } else {
                    (spec.hit(meter, cost, now), limiter.check_cost_at(&k, cost, now).unwrap())
                };
                total += 1;
                same += (want == got) as u64;
                if want != got {
                    *meter = None;
                    meters.remove(&k);
                    break;
                }
            }
            // 300 clients in 4096 buckets: two of them sharing a tag in one bucket is possible, and rare.
            assert!(same + 1 >= total, "{same} of {total} equal for {limit}/{window_ms}/{burst}");
        }
    }
}

/// Crowded table and abusers: whatever it admits, the specification fed only those requests admits too.
#[test]
fn it_never_admits_more_than_the_policy() {
    for exclusive in [false, true] {
        for (keys, buckets) in [(50_000u64, 64usize), (50_000, 1024), (200_000, 4096), (5_000, 4096), (40, 1)] {
            let spec = Spec::new(20, 10_000, 20);
            let mut limiter = Limiter::with_buckets(Policy::new(20, 10_000, 20).unwrap(), buckets);
            let mut meters: HashMap<u64, Option<Meter>> = HashMap::new();
            let mut r = Rng(buckets as u64);
            let mut admitted = 0u64;
            for ms in 0..scale(30_000) {
                let now = T0 + ms;
                for i in 0..60 {
                    let k = if i % 3 == 0 { r.below(50.min(keys)) } else { r.below(keys) }; // a third from a few abusers
                    let cost = if i % 7 == 0 { 1 + r.below(3) } else { 1 };
                    let got = if exclusive && cost == 1 {
                        limiter.check_at_mut(&k, now).allowed
                    } else {
                        limiter.check_cost_at(&k, cost, now).unwrap().allowed
                    };
                    if got {
                        admitted += 1;
                        assert!(spec.hit(meters.entry(k).or_default(), cost, now).allowed, "over-admitted key {k} with {buckets} buckets");
                    }
                }
            }
            assert!(admitted > 0);
        }
    }
}

/// A decision is a consistent answer even for a client counted with others.
#[test]
fn shared_clients_get_decisions_that_add_up() {
    let policy = Policy::new(5, 60_000, 5).unwrap();
    let limiter = Limiter::with_buckets(policy, 1);
    for k in 0..200u64 {
        let before = limiter.peek_at(&k, T0);
        let d = limiter.check_at(&k, T0);
        assert_eq!(d.allowed, before.allowed);
        assert!(d.remaining <= 5 && d.reset_ms <= 60_000);
        assert_eq!(d.retry_after_ms.is_some(), !d.allowed);
        if d.allowed {
            assert_eq!(d.remaining + 1, before.remaining);
        }
    }
    let c = limiter.census(T0);
    assert_eq!(c.owned + c.shared, SLOTS as u64);
    assert!(c.shared >= 2);
}

/// The 48-bit clock wraps under the test, and nothing changes.
#[test]
fn decisions_are_the_same_across_the_wrap_of_the_clock() {
    let (limit, window_ms, burst) = (50_000_000u64, 86_400_000u64, 1000u64); // 5e7 ticks per millisecond
    let policy = Policy::new(limit, window_ms, burst).unwrap();
    assert!(policy.is_exact());
    let spec = Spec::new(limit, window_ms, burst);
    let limiter = Limiter::with_buckets(policy, 4096);
    let mut meters: HashMap<u64, Option<Meter>> = HashMap::new();
    let wrap_ms = (1u64 << 48) / limit;
    let mut now = wrap_ms - 3_000;
    let mut r = Rng(11);
    for _ in 0..scale(400_000) {
        now += r.below(2);
        let k = r.below(40);
        let cost = 1 + r.below(30);
        assert_eq!(limiter.check_cost_at(&k, cost, now).unwrap(), spec.hit(meters.entry(k).or_default(), cost, now), "at {now}");
    }
    assert!(cfg!(miri) || now > wrap_ms + 3_000, "the test must cross the wrap");
}

/// An entry that expired a whole turn of the clock ago is gone, not live again.
#[test]
fn an_expired_entry_does_not_come_back_after_a_turn_of_the_clock() {
    let policy = Policy::new(50_000_000, 86_400_000, 1000).unwrap();
    let limiter = Limiter::with_buckets(policy, 512);
    for k in 0..2000u64 {
        assert!(limiter.check_at(&k, T0).allowed);
    }
    let all = 512 * SLOTS as u64;
    assert!(limiter.census(T0).free < all - 1900);
    // A turn is 2^48 ticks. Walk it in sweep steps, with a request in each, as a live table would.
    let step_ms = (1u64 << SWEEP_SHIFT) / 50_000_000 + 1;
    let turn_ms = (1u64 << 48) / 50_000_000;
    let mut now = T0;
    // Half a turn after the first requests, and again a whole turn after, their words would read
    // as ahead of the clock, were they still there.
    for back in [T0 + turn_ms / 2 + 10, T0 + turn_ms + 1] {
        while now < back - 3 * step_ms {
            now += step_ms;
            limiter.check_at("ticker", now);
        }
        // The ticker's own entry has drained by then, too.
        assert_eq!(limiter.census(back).free, all, "at {back}");
        for k in 0..2000u64 {
            assert_eq!(limiter.peek_at(&k, back).remaining, 1000);
        }
    }
}

/// A table that was silent for most of a turn of the clock starts clean, and keeps what is
/// being written as it wakes up.
#[test]
fn a_long_silence_leaves_nothing_behind() {
    let policy = Policy::new(50_000_000, 86_400_000, 1000).unwrap();
    let all = 64 * SLOTS as u64;
    for silence in [(1u64 << 46) / 50_000_000 + 5, (1u64 << 47) / 50_000_000 + 5, 3 * ((1u64 << 46) / 50_000_000)] {
        let limiter = Limiter::with_buckets(policy, 64);
        for k in 0..200u64 {
            limiter.check_at(&k, T0);
        }
        limiter.check_at("first after the silence", T0 + silence);
        assert_eq!(limiter.census(T0 + silence).free, all - 1, "after {silence} ms");
        assert_eq!(limiter.peek_at("first after the silence", T0 + silence).remaining, 999);
    }
}

/// A policy too large for the exact clock is stricter than the specification, never looser.
#[test]
fn a_scaled_policy_is_never_looser_than_the_specification() {
    let (limit, window_ms, burst) = (40u64, 86_400_000u64, 2_000_000u64); // burst * window_ms is 2^47
    let policy = Policy::new(limit, window_ms, burst).unwrap();
    assert!(!policy.is_exact());
    let spec = Spec::new(limit, window_ms, burst);
    let limiter = Limiter::with_buckets(policy, 256);
    let mut meters: HashMap<u64, Option<Meter>> = HashMap::new();
    let mut r = Rng(5);
    let mut now = T0;
    for _ in 0..scale(200_000) {
        now += r.below(50);
        let k = r.below(20);
        let cost = 1 + r.below(burst / 3);
        if limiter.check_cost_at(&k, cost, now).unwrap().allowed {
            assert!(spec.hit(meters.entry(k).or_default(), cost, now).allowed, "over-admitted at {now}");
        }
    }
}

/// A clock that goes back never gives quota back, and this limiter waits where the specification counts on.
#[test]
fn a_clock_that_goes_back_is_stricter_never_looser() {
    let limiter = Limiter::with_buckets(Policy::new(3, 1000, 3).unwrap(), 64);
    assert!(limiter.check_at("a", T0).allowed);
    assert_eq!(limiter.check_at("a", T0 - 5_000), Decision { allowed: false, remaining: 0, reset_ms: 5_334, retry_after_ms: Some(4_667) });
    assert_eq!(limiter.check_at("a", T0).remaining, 1);
    // Far back, too: an hour is nowhere near half a turn of this clock.
    assert!(!limiter.check_at("a", T0 - 3_600_000).allowed);
    assert_eq!(limiter.census(T0).owned, 1);
}

/// The one thing a clock that goes back can undo: an entry that had expired by the latest time
/// seen, and whose slot went to someone else. Its client starts again, as in any store that expires.
#[test]
fn an_entry_that_expired_and_was_replaced_stays_gone_when_the_clock_goes_back() {
    let limiter = Limiter::with_buckets(Policy::new(1, 1000, 1).unwrap(), 1);
    assert!(limiter.check_at("a", T0).allowed); // expires at T0 + 1000
    assert!(!limiter.check_at("a", T0 + 999).allowed);
    assert!(limiter.check_at("b", T0 + 1000).allowed); // takes the slot "a" had
    assert_eq!(limiter.census(T0 + 1000), Census { owned: 1, shared: 0, free: SLOTS as u64 - 1 });
    // A millisecond back, "a" would still be denied, were its entry there.
    assert!(limiter.check_at("a", T0 + 999).allowed);
}

#[test]
fn policies_are_validated_as_the_specification_says() {
    assert_eq!(Policy::new(0, 1000, 1), Err(PolicyError::Zero));
    assert_eq!(Policy::new(1, 1000, 0), Err(PolicyError::Zero));
    assert_eq!(Policy::new(1, 999, 1), Err(PolicyError::WindowTooShort));
    assert!(Policy::new(52_124_995, 86_400_000, 52_124_995).is_ok());
    assert_eq!(Policy::new(52_125_995, 86_400_000, 52_125_995), Err(PolicyError::TooLarge));
    assert_eq!(Policy::new(u64::MAX, u64::MAX, u64::MAX), Err(PolicyError::TooLarge));
    let p: Policy = "5/15minute".parse().unwrap();
    assert_eq!((p.limit(), p.window_ms(), p.burst()), (5, 900_000, 5));
    for bad in ["", "/", "5/minutes", "5 /minute", "05/minute", "5/0minute", "5/Minute", "-5/minute", "5/week", "5"] {
        assert_eq!(bad.parse::<Policy>(), Err(PolicyError::Shorthand), "{bad:?}");
    }
}

#[test]
fn a_cost_is_from_one_to_the_burst() {
    let limiter = Limiter::with_buckets(Policy::new(10, 60_000, 4).unwrap(), 8);
    assert_eq!(limiter.check_cost_at("a", 0, T0), Err(CostError { cost: 0, burst: 4 }));
    assert_eq!(limiter.peek_cost_at("a", 5, T0), Err(CostError { cost: 5, burst: 4 }));
    assert_eq!(limiter.check_cost_at("a", 4, T0).unwrap().remaining, 0);
    assert_eq!(limiter.census(T0).owned, 1);
}

#[test]
fn the_table_fits_the_memory_asked_for() {
    let p = Policy::new(5, 60_000, 5).unwrap();
    for bytes in [128usize, 4096, 1 << 20, (1 << 24) + 100] {
        let got = Limiter::with_memory(p, bytes).memory_bytes();
        assert!(got <= bytes && got + bytes / 64 + 64 >= bytes, "{got} for {bytes}");
    }
    assert!(Limiter::with_clients(p, 600).memory_bytes() >= 100 * 64);
}

#[cfg(all(target_arch = "aarch64", not(miri)))]
#[test]
fn the_vector_scan_equals_the_scalar_scan() {
    let mut r = Rng(77);
    for _ in 0..300_000 {
        let now = if r.below(3) == 0 { TAT_MASK - r.below(4000) } else { r.below(1 << 48) };
        let mut w = [0u64; SLOTS];
        for x in w.iter_mut() {
            let tag = match r.below(3) {
                0 => 5u64,
                1 => 1,
                _ => 1 + r.below(0x7FFF),
            };
            let tat = now.wrapping_add(r.below(4000)).wrapping_sub(2000).wrapping_add(if r.below(3) == 0 { AHEAD } else { 0 }) & TAT_MASK;
            *x = match r.below(5) {
                0 => 0,
                _ => tag << TAG_SHIFT | (r.below(2) << 48) | tat,
            };
        }
        assert_eq!(simd::find_in(&w, 5, now), scalar::find_in(&w, 5, now));
        assert_eq!(simd::live_in(&w, now), scalar::live_in(&w, now));
    }
}

#[test]
fn concurrent_hits_on_one_key_admit_exactly_the_burst() {
    let limiter = Limiter::with_buckets(Policy::new(100, 60_000, 100).unwrap(), 8);
    let admitted = std::sync::atomic::AtomicU64::new(0);
    std::thread::scope(|s| {
        for _ in 0..8 {
            s.spawn(|| {
                for _ in 0..scale(10_000) {
                    if limiter.allow_at("one", T0) {
                        admitted.fetch_add(1, Relaxed);
                    }
                }
            });
        }
    });
    assert_eq!(admitted.load(Relaxed), 100);
}

/// Eight threads, crowded buckets, clients arriving and expiring: per client, never more than
/// the policy allows over the whole run.
#[test]
fn concurrent_clients_never_exceed_the_policy() {
    let (limit, window_ms, burst) = (10u64, 1_000u64, 10u64);
    for buckets in [1usize, 4, 64, 2048] {
        let limiter = Limiter::with_buckets(Policy::new(limit, window_ms, burst).unwrap(), buckets);
        let keys = 600u64;
        let counts: Vec<std::sync::atomic::AtomicU64> = (0..keys).map(|_| Default::default()).collect();
        let clock = std::sync::atomic::AtomicU64::new(T0);
        let run_ms = scale(3_000);
        std::thread::scope(|s| {
            for th in 0..8u64 {
                let (limiter, counts, clock) = (&limiter, &counts, &clock);
                s.spawn(move || {
                    let mut r = Rng(th + 1);
                    loop {
                        let ms = clock.load(Relaxed);
                        if ms >= T0 + run_ms {
                            break;
                        }
                        for _ in 0..scale(4000).min(200) {
                            let k = r.below(keys);
                            if limiter.allow_at(&k, ms) {
                                counts[k as usize].fetch_add(1, Relaxed);
                            }
                        }
                        if th == 0 {
                            clock.fetch_add(1, Relaxed);
                        }
                    }
                });
            }
        });
        let allowed = burst + run_ms * limit / window_ms;
        for (k, c) in counts.iter().enumerate() {
            assert!(c.load(Relaxed) <= allowed, "key {k} got {} of {allowed} with {buckets} buckets", c.load(Relaxed));
        }
    }
}

mod properties {
    use super::*;
    use proptest::prelude::*;

    /// `PROPTEST_CASES` to run more: 512 by default, and a handful under the interpreter.
    fn cases() -> u32 {
        if cfg!(miri) {
            return 4;
        }
        std::env::var("PROPTEST_CASES").ok().and_then(|n| n.parse().ok()).unwrap_or(512)
    }
    fn fixed() -> ahash::RandomState {
        ahash::RandomState::with_seeds(1, 2, 3, 4)
    }
    /// An exact policy, and steps of (key, milliseconds forward, cost as a fraction of the burst, peek).
    fn exact_policy() -> impl Strategy<Value = (u64, u64, u64)> {
        (1u64..=1_000_000, 1000u64..=100_000_000, 1u64..=10_000)
            .prop_filter("exact", |(l, w, b)| Policy::new(*l, *w, *b).is_ok_and(|p| p.is_exact()))
    }
    fn steps(keys: u64) -> impl Strategy<Value = Vec<(u64, u64, u64, bool)>> {
        prop::collection::vec((0..keys, 0u64..=1000, 0u64..=1000, any::<bool>()), 1..200)
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(cases()))]

        /// With room and a clock that goes forward, any policy and any traffic: the specification's numbers.
        #[test]
        fn equal_to_the_specification_with_room((limit, window_ms, burst) in exact_policy(), steps in steps(6), stretch in 1u64..=4) {
            let spec = Spec::new(limit, window_ms, burst);
            let limiter = Limiter::with_buckets_and_hasher(Policy::new(limit, window_ms, burst).unwrap(), 1 << 12, fixed());
            let mut meters: HashMap<u64, Option<Meter>> = HashMap::new();
            let mut now = T0;
            for (key, forward, weight, peek) in steps {
                // Steps on the scale of the policy: from nothing to several intervals.
                now += forward * stretch * window_ms / limit / 250;
                let cost = 1 + weight * (burst - 1) / 1000;
                let meter = meters.entry(key).or_default();
                if peek {
                    prop_assert_eq!(limiter.peek_cost_at(&key, cost, now).unwrap(), spec.peek(*meter, cost, now));
                } else {
                    prop_assert_eq!(limiter.check_cost_at(&key, cost, now).unwrap(), spec.hit(meter, cost, now));
                }
            }
        }

        /// One or two buckets, more clients than slots, and a clock that jumps both ways. Whatever
        /// is admitted, the specification admits too, in a store that forgets a meter once it has
        /// drained by a time it has seen: what a store that expires its buckets does.
        #[test]
        fn never_looser_than_the_specification_when_crowded(
            (limit, window_ms, burst) in exact_policy(),
            buckets in 1usize..=2,
            steps in prop::collection::vec((0u64..40, -300i64..=1000, 0u64..=1000), 1..300),
        ) {
            let spec = Spec::new(limit, window_ms, burst);
            let limiter = Limiter::with_buckets_and_hasher(Policy::new(limit, window_ms, burst).unwrap(), buckets, fixed());
            let mut meters: HashMap<u64, Option<Meter>> = HashMap::new();
            let (mut now, mut latest) = (T0, 0);
            for (key, forward, weight) in steps {
                now = now.saturating_add_signed(forward * (window_ms / limit / 100).max(1) as i64);
                latest = latest.max(now);
                let cost = 1 + weight * (burst - 1) / 1000;
                let meter = meters.entry(key).or_default();
                if meter.is_some_and(|m| m.level as u128 <= latest.saturating_sub(m.ts) as u128 * limit as u128) {
                    *meter = None;
                }
                if limiter.check_cost_at(&key, cost, now).unwrap().allowed {
                    prop_assert!(spec.hit(meter, cost, now).allowed, "over-admitted key {} at {}", key, now);
                }
            }
        }
    }
}
