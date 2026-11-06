-- omnilimit state format 1. Apply once with your migration tool; omnilimit never runs it for you.
CREATE TABLE IF NOT EXISTS omnilimit_state (
  key           text PRIMARY KEY,
  level         bigint NOT NULL,
  ts_ms         bigint NOT NULL,
  expires_at_ms bigint NOT NULL
);

CREATE INDEX IF NOT EXISTS omnilimit_state_expires_idx ON omnilimit_state (expires_at_ms);

CREATE OR REPLACE FUNCTION omnilimit_hit_v1(
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
  v_level bigint;
  v_ts bigint;
  v_now bigint;
  v_t bigint;
  v_drained bigint;
BEGIN
  -- The placeholder (level 0, ts 0) behaves exactly like a missing row.
  INSERT INTO omnilimit_state AS s (key, level, ts_ms, expires_at_ms)
  VALUES (p_key, 0, 0, 0)
  ON CONFLICT (key) DO NOTHING;

  SELECT s.level, s.ts_ms INTO v_level, v_ts
  FROM omnilimit_state AS s
  WHERE s.key = p_key
  FOR UPDATE;

  -- Read the clock after taking the lock: waiters must not act on a stale time.
  v_now := COALESCE(p_now_ms, floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint);
  v_t := GREATEST(v_now, v_ts);
  v_drained := GREATEST(0, v_level - LEAST(v_t - v_ts, p_drain_ms) * p_limit);

  IF v_drained + p_window_ms > p_capacity THEN
    RETURN QUERY SELECT false, v_drained, v_ts;
    RETURN;
  END IF;

  UPDATE omnilimit_state AS s
  SET level = v_drained + p_window_ms,
      ts_ms = v_t,
      expires_at_ms = v_t + p_drain_ms
  WHERE s.key = p_key;

  RETURN QUERY SELECT true, v_drained + p_window_ms, v_t;
END;
$$;

CREATE OR REPLACE FUNCTION omnilimit_cleanup_v1(p_batch integer DEFAULT 1000, p_now_ms bigint DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
  v_deleted integer;
  v_now bigint := COALESCE(p_now_ms, floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint);
BEGIN
  DELETE FROM omnilimit_state
  WHERE key IN (
    SELECT s.key FROM omnilimit_state AS s
    WHERE s.expires_at_ms < v_now
    ORDER BY s.expires_at_ms
    LIMIT p_batch
    FOR UPDATE SKIP LOCKED
  );
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  RETURN v_deleted;
END;
$$;
