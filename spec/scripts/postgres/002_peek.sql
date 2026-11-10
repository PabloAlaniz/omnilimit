-- omnilimit state format 1: read-only peek. Apply after 001_v1.sql; it is idempotent.
-- Returns the decision a hit would get now without writing or locking. It is advisory:
-- the hit that follows is still the authority.
CREATE OR REPLACE FUNCTION omnilimit_peek_v1(
  p_key text,
  p_limit bigint,
  p_window_ms bigint,
  p_capacity bigint,
  p_drain_ms bigint,
  p_now_ms bigint DEFAULT NULL
) RETURNS TABLE (allowed boolean, new_level bigint, new_ts bigint)
LANGUAGE plpgsql
AS $$
DECLARE
  v_level bigint := 0;
  v_ts bigint;
  v_now bigint := COALESCE(p_now_ms, floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint);
  v_drained bigint;
BEGIN
  SELECT s.level, s.ts_ms INTO v_level, v_ts
  FROM omnilimit_state AS s
  WHERE s.key = p_key;

  IF NOT FOUND THEN
    v_level := 0;
    v_ts := v_now;
  END IF;

  v_drained := GREATEST(0, v_level - LEAST(GREATEST(v_now, v_ts) - v_ts, p_drain_ms) * p_limit);
  RETURN QUERY SELECT v_drained + p_window_ms <= p_capacity, v_drained, v_ts;
END;
$$;
