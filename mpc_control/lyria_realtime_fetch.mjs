#!/usr/bin/env node
import fs from "node:fs";

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk, "utf8") : chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function sanitizeError(msg) {
  return String(msg || "unknown error").replace(/key=[^&\s"']+/gi, "key=REDACTED");
}

async function decodeMessageData(data) {
  if (typeof data === "string") return data;
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString("utf8");
  if (ArrayBuffer.isView(data)) {
    return Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString("utf8");
  }
  if (data && typeof data.text === "function") return await data.text();
  return String(data ?? "");
}

async function main() {
  if (typeof WebSocket !== "function") {
    throw new Error("Node built-in WebSocket is unavailable in this runtime");
  }
  const rawInput = (await readStdin()).trim();
  if (!rawInput) {
    throw new Error("Expected JSON payload on stdin");
  }
  const req = JSON.parse(rawInput);
  const apiKey = String(req.api_key || "").trim();
  if (!apiKey) {
    throw new Error("Missing api_key");
  }
  const prompt = String(req.prompt || "").trim();
  if (!prompt) {
    throw new Error("Missing prompt");
  }
  const outPcm = String(req.out_pcm || "").trim();
  if (!outPcm) {
    throw new Error("Missing out_pcm path");
  }

  const modelRaw = String(req.model || "models/lyria-realtime-exp").trim();
  const model = modelRaw.startsWith("models/") ? modelRaw : `models/${modelRaw}`;
  const durationS = Math.max(1.0, Math.min(60.0, Number(req.duration_s || 12.0)));
  const targetBytes = Math.ceil(durationS * 48000 * 2 * 2);
  const timeoutMs = Math.max(5000, Number(req.timeout_ms || 45000));

  const genConfig = {
    musicGenerationMode: String(req.mode || "QUALITY"),
    temperature: req.temperature != null ? Number(req.temperature) : 1.0,
    guidance: req.guidance != null ? Number(req.guidance) : 4.0,
  };
  if (req.bpm != null && Number.isFinite(Number(req.bpm))) {
    genConfig.bpm = Math.max(60, Math.min(200, Math.round(Number(req.bpm))));
  }
  if (req.scale) {
    genConfig.scale = String(req.scale).trim();
  }

  const url =
    "wss://generativelanguage.googleapis.com/ws/" +
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateMusic" +
    `?key=${encodeURIComponent(apiKey)}`;

  await new Promise((resolve, reject) => {
    const audioBuffers = [];
    let bytesReceived = 0;
    let chunkCount = 0;
    let mimeType = "audio/l16;rate=48000;channels=2";
    let setupDone = false;
    let finished = false;

    const ws = new WebSocket(url);

    const timer = setTimeout(() => {
      if (finished) return;
      finished = true;
      try {
        ws.close();
      } catch {}
      reject(
        new Error(
          `Lyria RealTime timed out after ${timeoutMs}ms (chunks=${chunkCount}, bytes=${bytesReceived})`
        )
      );
    }, timeoutMs);

    const finishSuccess = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      const combined = Buffer.concat(audioBuffers);
      fs.writeFileSync(outPcm, combined);
      try {
        ws.send(JSON.stringify({ playbackControl: "STOP" }));
      } catch {}
      try {
        ws.close(1000, "done");
      } catch {}
      process.stdout.write(
        JSON.stringify({
          ok: true,
          model,
          chunks: chunkCount,
          bytes: combined.length,
          sample_rate: 48000,
          channels: 2,
          mime_type: mimeType,
        }) + "\n"
      );
      resolve();
    };

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ setup: { model } }));
    });

    ws.addEventListener("message", async (event) => {
      if (finished) return;
      try {
        const text = await decodeMessageData(event.data);
        const msg = JSON.parse(text);
        if (msg.error) {
          throw new Error(msg.error.message || JSON.stringify(msg.error));
        }
        if (msg.setupComplete && !setupDone) {
          setupDone = true;
          ws.send(
            JSON.stringify({
              clientContent: {
                weightedPrompts: [{ text: prompt, weight: 1.0 }],
              },
            })
          );
          ws.send(JSON.stringify({ musicGenerationConfig: genConfig }));
          ws.send(JSON.stringify({ playbackControl: "PLAY" }));
          return;
        }
        const chunks = msg.serverContent?.audioChunks;
        if (Array.isArray(chunks)) {
          for (const ch of chunks) {
            if (!ch?.data) continue;
            if (ch.mimeType) mimeType = String(ch.mimeType);
            const raw = Buffer.from(ch.data, "base64");
            audioBuffers.push(raw);
            bytesReceived += raw.length;
            chunkCount += 1;
          }
          if (bytesReceived >= targetBytes) {
            finishSuccess();
          }
        }
      } catch (err) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        try {
          ws.close();
        } catch {}
        reject(err);
      }
    });

    ws.addEventListener("error", () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      reject(new Error("Lyria RealTime WebSocket transport error"));
    });

    ws.addEventListener("close", (ev) => {
      if (finished) return;
      if (bytesReceived > 0 && bytesReceived >= Math.floor(targetBytes * 0.5)) {
        finishSuccess();
        return;
      }
      finished = true;
      clearTimeout(timer);
      reject(
        new Error(
          `Lyria RealTime WebSocket closed early (code=${ev.code}, reason=${sanitizeError(ev.reason)})`
        )
      );
    });
  });
}

main().catch((err) => {
  process.stderr.write(sanitizeError(err?.stack || err?.message || err) + "\n");
  process.exit(1);
});
