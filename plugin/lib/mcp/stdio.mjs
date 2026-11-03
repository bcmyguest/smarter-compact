/** MCP stdio framing (NDJSON + Content-Length). Shared by unified and provider MCP servers. */

/** @param {unknown} obj @param {"ndjson" | "content-length"} framing */
export function writeMessage(obj, framing = "ndjson") {
  if (obj == null) return;
  const body = JSON.stringify(obj);
  if (framing === "content-length") {
    process.stdout.write(`Content-Length: ${Buffer.byteLength(body, "utf8")}\r\n\r\n${body}`);
    return;
  }
  process.stdout.write(`${body}\n`);
}

/** @param {AsyncIterable<Buffer | string>} input @param {(msg: Record<string, unknown>) => Promise<unknown>} onMsg */
export async function serveStdio(input, onMsg) {
  let buf = Buffer.alloc(0);
  for await (const chunk of input) {
    buf = Buffer.concat([buf, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)]);
    while (buf.length) {
      const asStr = buf.toString("utf8");
      const crlf = asStr.indexOf("\r\n\r\n");
      const lf = asStr.indexOf("\n\n");
      const headerEnd = crlf >= 0 ? crlf : lf;
      const sepLen = crlf >= 0 ? 4 : lf >= 0 ? 2 : -1;
      if (sepLen > 0 && /content-length\s*:/i.test(asStr.slice(0, headerEnd))) {
        const m = /Content-Length:\s*(\d+)/i.exec(asStr.slice(0, headerEnd));
        if (!m) {
          buf = buf.subarray(Buffer.byteLength(asStr.slice(0, headerEnd + sepLen), "utf8"));
          continue;
        }
        const len = Number(m[1]);
        const headerBytes = Buffer.byteLength(asStr.slice(0, headerEnd + sepLen), "utf8");
        if (buf.length < headerBytes + len) break;
        const body = buf.subarray(headerBytes, headerBytes + len).toString("utf8");
        buf = buf.subarray(headerBytes + len);
        let msg;
        try {
          msg = JSON.parse(body);
        } catch (err) {
          writeMessage(
            {
              jsonrpc: "2.0",
              id: null,
              error: { code: -32700, message: `Parse error: ${err instanceof Error ? err.message : err}` },
            },
            "content-length",
          );
          continue;
        }
        writeMessage(await onMsg(msg), "content-length");
        continue;
      }
      const nl = asStr.indexOf("\n");
      if (nl < 0) break;
      const line = asStr.slice(0, nl).trim();
      buf = buf.subarray(Buffer.byteLength(asStr.slice(0, nl + 1), "utf8"));
      if (!line.startsWith("{")) continue;
      try {
        writeMessage(await onMsg(JSON.parse(line)), "ndjson");
      } catch {
        /* skip bad line */
      }
    }
  }
}
