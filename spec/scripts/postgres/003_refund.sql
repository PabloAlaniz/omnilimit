-- omnilimit state format 1: refund. Apply after 001_v1.sql; it is idempotent.
-- p_window_ms is the reservation's cost times the window, and p_since_ms the ts its hit stored.
-- Gives back what is left of the reservation after draining since then, never below zero.
-- p_capacity is unused: it keeps the arguments of the hit, in the same order.
CREATE OR REPLACE FUNCTION omnilimit_refund_v1(
  p_key text,
  p_limit bigint,
  p_window_ms bigint,
  p_capacity bigint,
  p_drain_ms bigint,
  p_since_ms bigint,
  p_now_ms bigint DEFAULT NULL
) RETURNS TABLE (new_level bigint, new_ts bigint)
LANGUAGE plpgsql
AS $$
DECLARE
  v_level bigint;
  v_ts bigint;
  v_found boolean;
  v_now bigint;
  v_t bigint;
  v_drained bigint;
  v_give bigint := 0;
BEGIN
  SELECT s.level, s.ts_ms INTO v_level, v_ts
  FROM omnilimit_state AS s
  WHERE s.key = p_key
  FOR UPDATE;
  v_found := FOUND;

  -- Read the clock after taking the lock: waiters must not act on a stale time.
  v_now := COALESCE(p_now_ms, floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint);

  IF NOT v_found THEN
    -- The bucket is gone, and the reservation with it.
    RETURN QUERY SELECT 0::bigint, v_now;
    RETURN;
  END IF;

  v_t := GREATEST(v_now, v_ts);
  v_drained := GREATEST(0, v_level - LEAST(v_t - v_ts, p_drain_ms) * p_limit);
  IF v_t >= p_since_ms THEN
    v_give := GREATEST(0, p_window_ms - LEAST(v_t - p_since_ms, p_drain_ms) * p_limit);
  END IF;

  IF v_drained = 0 OR v_give = 0 THEN
    RETURN QUERY SELECT v_drained, v_ts;
    RETURN;
  END IF;

  UPDATE omnilimit_state AS s
  SET level = GREATEST(0, v_drained - v_give),
      ts_ms = v_t,
      expires_at_ms = v_t + p_drain_ms
  WHERE s.key = p_key;

  RETURN QUERY SELECT GREATEST(0, v_drained - v_give), v_t;
END;
$$;
