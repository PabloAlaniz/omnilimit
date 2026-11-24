// A TCP proxy between a client and a real store that can lose what a network loses: the reply,
// the request, or the connection in the middle of a call.
import { createServer, connect, type Socket } from 'node:net';
import type { AddressInfo } from 'node:net';

type Mode = 'pass' | 'hold-replies' | 'hold-requests';

interface Link {
  client: Socket;
  upstream: Socket;
  toUpstream: Buffer[];
  toClient: Buffer[];
}

export interface FaultProxy {
  /** The store's URL with the proxy's host and port. */
  url: string;
  /** Requests still reach the store, and its replies wait here. */
  holdReplies(): void;
  /** Requests wait here: the store sees nothing. */
  holdRequests(): void;
  /** Deliver what was held, in order, and forward again. */
  resume(): void;
  /** Close every connection. What was held is lost, and new connections are forwarded. */
  cut(): void;
  /** Bytes of replies held so far: above zero, the store has answered a call nobody heard. */
  heldReplyBytes(): number;
  heldRequestBytes(): number;
  close(): Promise<void>;
}

export async function faultProxy(target: string): Promise<FaultProxy> {
  const url = new URL(target);
  const port = Number(url.port);
  const host = url.hostname;
  const links = new Set<Link>();
  let mode: Mode = 'pass';

  const server = createServer((client) => {
    const upstream = connect({ host, port });
    const link: Link = { client, upstream, toUpstream: [], toClient: [] };
    links.add(link);
    client.on('data', (chunk) => {
      if (mode === 'hold-requests') link.toUpstream.push(chunk);
      else upstream.write(chunk);
    });
    upstream.on('data', (chunk) => {
      if (mode === 'hold-replies') link.toClient.push(chunk);
      else client.write(chunk);
    });
    const drop = () => {
      links.delete(link);
      client.destroy();
      upstream.destroy();
    };
    for (const socket of [client, upstream]) {
      socket.on('error', drop);
      socket.on('close', drop);
    }
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  url.hostname = '127.0.0.1';
  url.port = String((server.address() as AddressInfo).port);

  const total = (pick: (link: Link) => Buffer[]) => [...links].reduce((sum, link) => sum + pick(link).reduce((n, chunk) => n + chunk.length, 0), 0);
  return {
    url: url.toString(),
    holdReplies: () => void (mode = 'hold-replies'),
    holdRequests: () => void (mode = 'hold-requests'),
    resume() {
      mode = 'pass';
      for (const link of links) {
        for (const chunk of link.toUpstream.splice(0)) link.upstream.write(chunk);
        for (const chunk of link.toClient.splice(0)) link.client.write(chunk);
      }
    },
    cut() {
      mode = 'pass';
      for (const link of [...links]) {
        link.client.destroy();
        link.upstream.destroy();
      }
      links.clear();
    },
    heldReplyBytes: () => total((link) => link.toClient),
    heldRequestBytes: () => total((link) => link.toUpstream),
    close: () =>
      new Promise<void>((resolve) => {
        for (const link of links) {
          link.client.destroy();
          link.upstream.destroy();
        }
        server.close(() => resolve());
      }),
  };
}

/** Poll until `done` holds, so a test waits for an event and not for a guessed delay. */
export async function until(done: () => boolean | Promise<boolean>, what: string, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!(await done())) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
