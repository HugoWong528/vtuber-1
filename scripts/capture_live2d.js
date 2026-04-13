#!/usr/bin/env node
/**
 * capture_live2d.js
 * =================
 * Renders the Live2D Miku model in headless Chromium via Puppeteer and
 * encodes the animation into an MP4 video using an FFmpeg child process.
 *
 * Usage:
 *   node capture_live2d.js <server_port> <output_mp4> <duration_secs> <fps>
 *
 * Environment:
 *   Expects a static HTTP server already running at localhost:<server_port>
 *   that serves the repository root (so /miku_sample_t04.model3.json etc.
 *   are reachable).
 *
 * Strategy:
 *   Screenshots are taken with page.screenshot() which returns PNG buffers.
 *   These are piped directly into FFmpeg via -f image2pipe -vcodec png, which
 *   avoids any per-frame JSON serialisation and keeps throughput high.
 */

'use strict';

const puppeteer = require('puppeteer');
const { spawn }  = require('child_process');
const process    = require('process');

// ── CLI arguments ────────────────────────────────────────────────────────────
const [,, serverPort, outputMp4, durationStr, fpsStr] = process.argv;

if (!serverPort || !outputMp4 || !durationStr || !fpsStr) {
  console.error('Usage: node capture_live2d.js <port> <output.mp4> <duration_secs> <fps>');
  process.exit(1);
}

const DURATION     = parseFloat(durationStr);
const FPS          = parseInt(fpsStr, 10);
const WIDTH        = 1080;
const HEIGHT       = 1920;
const FRAME_MS     = Math.round(1000 / FPS);
const TOTAL_FRAMES = Math.ceil(DURATION * FPS);

const CAPTURE_URL = `http://localhost:${serverPort}/scripts/live2d_capture.html`;

// ── FFmpeg child process (reads PNG images piped through stdin) ───────────────
function startFFmpeg(output, fps) {
  const args = [
    '-y',
    '-f', 'image2pipe',
    '-framerate', String(fps),
    '-vcodec', 'png',
    '-i', 'pipe:0',
    '-c:v', 'libx264',
    '-preset', 'slow',
    '-crf', '18',
    '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart',
    output,
  ];
  const ff = spawn('ffmpeg', args, { stdio: ['pipe', 'inherit', 'inherit'] });
  ff.on('error', (err) => { console.error('[FFmpeg error]', err); process.exit(1); });
  return ff;
}

// ── Main ─────────────────────────────────────────────────────────────────────
(async () => {
  console.log(`[capture] Launching headless browser — ${TOTAL_FRAMES} frames @ ${FPS}fps (${DURATION}s)`);

  const browser = await puppeteer.launch({
    headless: true,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--enable-webgl',
      '--use-gl=angle',
      '--use-angle=swiftshader',
    ],
  });

  const page = await browser.newPage();

  // Exact pixel viewport with no scaling
  await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 });

  console.log(`[capture] Navigating to ${CAPTURE_URL}`);
  await page.goto(CAPTURE_URL, { waitUntil: 'networkidle0', timeout: 60_000 });

  // Wait until the Live2D model has finished loading (or errored)
  console.log('[capture] Waiting for Live2D model to load …');
  await page.waitForFunction(
    () => window.modelReady === true || window.modelError !== null,
    { timeout: 60_000 }
  );

  const errMsg = await page.evaluate(() => window.modelError);
  if (errMsg) {
    console.error('[capture] Model failed to load:', errMsg);
    await browser.close();
    process.exit(1);
  }
  console.log('[capture] Model ready — starting frame capture');

  // The browser-side warm-up (90 frames of physics simulation) already ran
  // inside init() before window.modelReady was set to true, so we can start
  // recording immediately without an additional real-time sleep.

  const ff = startFFmpeg(outputMp4, FPS);

  // Capture each frame as a PNG and pipe directly into FFmpeg.
  //
  // Key change for smooth output:
  //   window.advanceFrame(ms) steps the Live2D animation forward by exactly
  //   `ms` milliseconds using a fixed virtual clock, then synchronously
  //   triggers a PIXI render.  This decouples animation advancement from
  //   wall-clock time, so every frame represents a uniform slice of animation
  //   regardless of how long the screenshot round-trip takes.  The old approach
  //   of setTimeout(FRAME_MS) *after* each screenshot produced irregular frame
  //   spacing (screenshot_time + FRAME_MS ≠ FRAME_MS), which is what caused
  //   the visible jitter / shaking in the output video.
  for (let i = 0; i < TOTAL_FRAMES; i++) {
    // Advance the Live2D model and PIXI renderer by exactly one frame period.
    await page.evaluate((ms) => window.advanceFrame(ms), FRAME_MS);

    const pngBuf = await page.screenshot({ type: 'png', omitBackground: false });
    ff.stdin.write(pngBuf);

    if ((i + 1) % FPS === 0 || i === TOTAL_FRAMES - 1) {
      const elapsed = ((i + 1) / FPS).toFixed(1);
      console.log(`[capture] ${i + 1}/${TOTAL_FRAMES} frames (${elapsed}s)`);
    }
  }

  ff.stdin.end();

  await new Promise((resolve, reject) => {
    ff.on('close', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`FFmpeg exited with code ${code}`));
    });
  });

  await browser.close();
  console.log(`[capture] Done — ${outputMp4}`);
})().catch((err) => {
  console.error('[capture] Fatal error:', err);
  process.exit(1);
});
