// Minimal surface of `cloudflare:workers`, so building does not need @cloudflare/workers-types.
declare module 'cloudflare:workers' {
  export abstract class DurableObject {
    protected ctx: {
      storage: {
        get<T>(key: string): Promise<T | undefined>;
        put<T>(key: string, value: T): Promise<void>;
        deleteAll(): Promise<void>;
        setAlarm(scheduledTime: number): Promise<void>;
      };
    };
    constructor(ctx: unknown, env: unknown);
    alarm?(): Promise<void>;
  }
}
