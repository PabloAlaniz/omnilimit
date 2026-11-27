//! Every interleaving of a few threads on one bucket of two slots, by the loom model checker:
//!
//!   RUSTFLAGS="--cfg loom" cargo test -p omnilimit-kernel --release --test loom
//!
//! Each model states what must hold whatever the order of the reads, compare-and-swaps and locks.
#![cfg(loom)]

use loom::sync::Arc;
use loom::thread;
use omnilimit_kernel::{Limiter, Policy};

const T0: u64 = 1_700_000_000_000;
type L = Limiter<ahash::RandomState>;

/// One bucket, and a hasher that is the same on every run of a model.
fn limiter(limit: u64, burst: u64) -> Arc<L> {
    Arc::new(Limiter::with_buckets_and_hasher(Policy::new(limit, 60_000, burst).unwrap(), 1, ahash::RandomState::with_seeds(1, 2, 3, 4)))
}
fn race(a: impl FnOnce() -> bool + Send + 'static, b: impl FnOnce() -> bool + Send + 'static) -> (bool, bool) {
    let t = thread::spawn(a);
    let b = b();
    (t.join().unwrap(), b)
}

/// Two threads bring the same new client: it gets one slot, and its burst of one once.
#[test]
fn the_same_new_client_from_two_threads_is_counted_once() {
    loom::model(|| {
        let l = limiter(1, 1);
        let (l1, l2) = (l.clone(), l.clone());
        let (a, b) = race(move || l1.allow_at("k", T0), move || l2.allow_at("k", T0));
        assert!(a ^ b);
        let c = l.census(T0);
        assert_eq!((c.owned, c.shared), (1, 0));
    });
}

/// Two threads count the same known client: no update is lost.
#[test]
fn two_hits_on_a_known_client_are_both_counted() {
    loom::model(|| {
        let l = limiter(3, 3);
        assert!(l.allow_at("k", T0));
        let (l1, l2) = (l.clone(), l.clone());
        let (a, b) = race(move || l1.allow_at("k", T0), move || l2.allow_at("k", T0));
        assert!(a && b);
        assert!(!l.allow_at("k", T0));
        assert_eq!(l.peek_at("k", T0).remaining, 0);
    });
}

/// One unit left for a known client and two threads want it.
#[test]
fn the_last_unit_goes_to_one_of_two_threads() {
    loom::model(|| {
        let l = limiter(2, 2);
        assert!(l.allow_at("k", T0));
        let (l1, l2) = (l.clone(), l.clone());
        let (a, b) = race(move || l1.allow_at("k", T0), move || l2.allow_at("k", T0));
        assert!(a ^ b);
    });
}

/// The bucket is full and two threads bring the same newcomer: it is counted in the shared slots
/// once, and its second request finds the first.
#[test]
fn a_newcomer_to_a_full_bucket_is_counted_once() {
    loom::model(|| {
        let l = limiter(1, 1);
        assert!(l.allow_at("x", T0) && l.allow_at("y", T0));
        let (l1, l2) = (l.clone(), l.clone());
        let (a, b) = race(move || l1.allow_at("n", T0), move || l2.allow_at("n", T0));
        assert!(!(a && b));
        assert!(!l.allow_at("n", T0));
    });
}

/// An owner counts a request while a newcomer is being added to its slot: neither write is lost.
#[test]
fn an_owner_and_a_newcomer_on_one_slot_keep_both_counts() {
    loom::model(|| {
        let l = limiter(2, 2);
        assert!(l.allow_at("x", T0) && l.allow_at("y", T0));
        let (l1, l2) = (l.clone(), l.clone());
        let (owner, newcomer) = race(move || l1.allow_at("x", T0), move || l2.allow_at("n", T0));
        assert!(owner && newcomer);
        // The owner has used its two; the newcomer, counted next to owners at one or two, has at most one left.
        assert!(!l.allow_at("x", T0));
        let more = l.allow_at("n", T0) as u32 + l.allow_at("n", T0) as u32;
        assert!(more <= 1);
        assert_eq!(l.census(T0).shared, 2);
    });
}

/// A client's entry is about to expire. One thread, a millisecond behind, still counts on it;
/// another, a millisecond ahead, gives the slot to someone else. Whoever wins, no request that
/// was admitted goes unrecorded.
#[test]
fn a_slot_reused_under_a_thread_that_is_behind_loses_nothing() {
    loom::model(|| {
        let l = limiter(60_000, 2); // one request per millisecond, two at once
        assert!(l.allow_at("z", T0 + 1)); // the other slot, live for both threads
        assert!(l.allow_at("k", T0)); // expires at T0 + 1
        let (l1, l2) = (l.clone(), l.clone());
        let (k, n) = race(move || l1.allow_at("k", T0), move || l2.allow_at("n", T0 + 1));
        assert!(k && n);
        // At T0, "k" has used both of its two. At T0 + 1, "n" has used one of two.
        assert!(!l.allow_at("k", T0));
        let more = l.allow_at("n", T0 + 1) as u32 + l.allow_at("n", T0 + 1) as u32;
        assert!(more <= 1);
    });
}
