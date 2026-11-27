//! The conformance vectors of `spec/vectors` that this limiter can express.
//!
//! It runs the hits and peeks of every `sequence` vector, of any cost, on a table with room, and
//! compares the four public numbers of each decision. A vector is followed up to its first reset
//! or refund, which this limiter does not have, and up to the first step whose clock goes back,
//! where it differs from the specification on purpose (see the crate documentation). A policy
//! too large for the exact clock is only checked for admitting nothing the specification denies.
#![cfg(not(loom))]

use omnilimit_kernel::{CostError, Limiter, Policy};
use serde_json::Value;
use std::path::PathBuf;

fn vectors(kind: &str) -> Vec<Value> {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/vectors").join(kind);
    let mut paths: Vec<PathBuf> =
        std::fs::read_dir(&dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())).map(|e| e.unwrap().path()).collect();
    paths.sort();
    paths.iter().map(|p| serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap()).collect()
}
fn policy_of(v: &Value) -> Result<Policy, omnilimit_kernel::PolicyError> {
    let p = &v["policy"];
    let limit = p["limit"].as_u64().unwrap();
    Policy::new(limit, p["window_ms"].as_u64().unwrap(), p["burst"].as_u64().unwrap_or(limit))
}

#[test]
fn sequence_vectors() {
    let (mut whole, mut cut, mut steps_equal, mut steps_strict) = (0, 0, 0u64, 0u64);
    for v in vectors("sequence") {
        if v.get("shadow").is_some() {
            continue;
        } // shadow mode belongs to the limiter around a store
        let id = v["id"].as_str().unwrap();
        let policy = policy_of(&v).unwrap();
        let limiter = Limiter::with_buckets(policy, 1 << 14);
        let exact = policy.is_exact();
        let mut latest = 0u64;
        let mut complete = true;
        for step in v["steps"].as_array().unwrap() {
            let op = step.get("op").and_then(Value::as_str).unwrap_or("hit");
            if op == "reset" || op == "refund" {
                complete = false;
                break;
            }
            let (at, key, cost) =
                (step["at_ms"].as_u64().unwrap(), step["id"].as_str().unwrap(), step.get("cost").and_then(Value::as_u64).unwrap_or(1));
            if at < latest {
                complete = false;
                break;
            }
            latest = at;
            let got = if op == "peek" { limiter.peek_cost_at(key, cost, at) } else { limiter.check_cost_at(key, cost, at) }.unwrap();
            let want = &step["expect"];
            if exact {
                assert_eq!(got.allowed, want["allowed"].as_bool().unwrap(), "{id}: allowed at {at}");
                assert_eq!(got.remaining, want["remaining"].as_u64().unwrap(), "{id}: remaining at {at}");
                assert_eq!(got.reset_ms, want["reset_ms"].as_u64().unwrap(), "{id}: reset_ms at {at}");
                assert_eq!(got.retry_after_ms, want["retry_after_ms"].as_u64(), "{id}: retry_after_ms at {at}");
                steps_equal += 1;
            } else {
                // Stricter is allowed, looser is not. Once it denied what the vector counted, the two
                // hold different states and the rest of the vector says nothing about this limiter.
                assert!(!got.allowed || want["allowed"].as_bool().unwrap(), "{id}: admitted at {at} what the specification denies");
                steps_strict += 1;
                if got.allowed != want["allowed"].as_bool().unwrap() {
                    complete = false;
                    break;
                }
            }
        }
        if complete {
            whole += 1
        } else {
            cut += 1
        }
    }
    println!("sequence vectors: {whole} followed to the end, {cut} cut short; {steps_equal} steps equal to the specification, {steps_strict} checked as not looser");
    assert!(whole >= 60 && steps_equal >= 1000, "the vectors moved: {whole} whole, {steps_equal} steps");
}

#[test]
fn concurrency_exactly_the_burst() {
    for v in vectors("concurrency") {
        // The others are about recipes built on reset and refund.
        if v["id"] != "concurrency/exactly-burst-allowed" {
            continue;
        }
        let limiter = Limiter::with_buckets(policy_of(&v).unwrap(), 64);
        let (hits, at, key) = (v["hits"].as_u64().unwrap(), v["at_ms"].as_u64().unwrap(), v["id_value"].as_str().unwrap());
        let allowed = std::sync::atomic::AtomicU64::new(0);
        std::thread::scope(|s| {
            for _ in 0..8 {
                s.spawn(|| {
                    for _ in 0..hits / 8 {
                        if limiter.allow_at(key, at) {
                            allowed.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                        }
                    }
                });
            }
        });
        assert_eq!(allowed.into_inner(), v["expect_allowed"].as_u64().unwrap());
        return;
    }
    panic!("concurrency/exactly-burst-allowed is gone");
}

#[test]
fn contract_policy_validation() {
    let mut checked = 0;
    for v in vectors("contract") {
        if v["subject"] != "policy" {
            continue;
        }
        for case in v["cases"].as_array().unwrap() {
            let p = &case["policy"];
            // A value that is not a non-negative integer cannot be written in this API at all.
            let (Some(limit), Some(window_ms), Some(burst)) = (p["limit"].as_u64(), p["window_ms"].as_u64(), p["burst"].as_u64()) else {
                assert_eq!(case["valid"], false, "{p}");
                continue;
            };
            assert_eq!(Policy::new(limit, window_ms, burst).is_ok(), case["valid"].as_bool().unwrap(), "{p}");
            checked += 1;
        }
    }
    assert!(checked >= 5);
}

#[test]
fn contract_policy_shorthand() {
    let mut checked = 0;
    for v in vectors("contract") {
        if v["subject"] != "shorthand" {
            continue;
        }
        for case in v["cases"].as_array().unwrap() {
            let text = case["text"].as_str().unwrap();
            let got = text.parse::<Policy>().ok().map(|p| (p.limit(), p.window_ms()));
            let want = case["policy"].as_object().map(|p| (p["limit"].as_u64().unwrap(), p["window_ms"].as_u64().unwrap()));
            assert_eq!(got, want, "{text:?}");
            checked += 1;
        }
    }
    assert!(checked >= 10);
}

#[test]
fn contract_cost_validation() {
    let mut checked = 0;
    for v in vectors("contract") {
        if v["subject"] != "cost" {
            continue;
        }
        let limiter = Limiter::with_buckets(policy_of(&v).unwrap(), 8);
        for case in v["cases"].as_array().unwrap() {
            let Some(cost) = case["cost"].as_u64() else {
                assert_eq!(case["valid"], false, "{}", case["cost"]);
                continue;
            };
            let got = limiter.peek_cost_at("a", cost, 1_700_000_000_000);
            assert_eq!(got.is_ok(), case["valid"].as_bool().unwrap(), "cost {cost}");
            if let Err(e) = got {
                assert_eq!(e, CostError { cost, burst: limiter.policy().burst() });
            }
            checked += 1;
        }
    }
    assert!(checked >= 5);
}
