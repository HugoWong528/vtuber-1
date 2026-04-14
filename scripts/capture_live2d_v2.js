#!/usr/bin/env node
/**
 * capture_live2d_v2.js
 * ====================
 * V2 of the Live2D Miku capture script.
 *
 * Enhancements over capture_live2d.js:
 *   • Accepts a 7th CLI argument: JSON-encoded motion-cue schedule
 *     (array of {frameIndex, group, motionIndex}).
 *   • Enables speaking / lip-sync mode throughout the capture (the mouth
 *     oscillates via window.setSpeaking(true) in live2d_capture_v2.html).
 *   • At the frame index specified in each cue, calls window.triggerMotion()
 *     so the VTuber reacts to the content she is speaking.
 *
 * Usage:
 *   node capture_live2d_v2.js <port> <output.mp4> <duration_secs> <fps>
 *                              [ffmpeg_preset] [motion_cues_json]
 *
 * motion_cues_json example:
 *   '[{"frameIndex":0,"group":"Wave","motionIndex":0},
 *     {"frameIndex":60,"group":"FlickUp","motionIndex":0}]'
 */

'use strict';

const puppeteer = require('puppeteer');
const { spawn }  = require('child_process');
const process    = require('process');

// ── CLI arguments ────────────────────────────────────────────────────────────
const [,, serverPort, outputMp4, durationStr, fpsStr, presetArg, motionCuesArg] = process.argv;

if (!serverPort || !outputMp4 || !durationStr || !fpsStr) {
  console.error(
    'Usage: node capture_live2d_v2.js <port> <output.mp4> <duration_secs> <fps>' +
    ' [ffmpeg_preset] [motion_cues_json]'
  );
  process.exit(1);
}

const FFMPEG_PRESET = presetArg || 'slow';

const DURATION     = parseFloat(durationStr);
const FPS          = parseInt(fpsStr, 10);
const WIDTH        = 1080;
const HEIGHT       = 1920;
const FRAME_MS     = Math.round(1000 / FPS);
const TOTAL_FRAMES = Math.ceil(DURATION * FPS);

// Parse motion cue schedule
let motionSchedule = [];
if (motionCuesArg) {
  try {
    motionSchedule = JSON.parse(motionCuesArg);
    console.log(`[capture-v2] Loaded ${motionSchedule.length} motion cue(s)`);
  } catch (e) {
    console.warn('[capture-v2] Failed to parse motion_cues_json:', e.message);
  }
}

// Build a Map<frameIndex → [{group, motionIndex}]> for O(1) lookup per frame
const cueMap = new Map();
for (const cue of motionSchedule) {
  const fi = Math.max(0, Math.round(cue.frameIndex));
  if (!cueMap.has(fi)) cueMap.set(fi, []);
  cueMap.get(fi).push({ group: cue.group, motionIndex: cue.motionIndex || 0 });
}

const CAPTURE_URL = `http://localhost:${serverPort}/scripts/live2d_capture_v2.html`;

// ── FFmpeg child process ──────────────────────────────────────────────────────
function startFFmpeg(output, fps) {
  const args = [
    '-y',
    '-f', 'image2pipe',
    '-framerate', String(fps),
    '-vcodec', 'png',
    '-i', 'pipe:0',
    '-c:v', 'libx264',
    '-preset', FFMPEG_PRESET,
    '-crf', '18',
    '-pix_fmt', 'yuv420p',
    '-movflags', '+faststart',
    output,
  ];
  const ff = spawn('ffmpeg', args, { stdio: ['pipe', 'inherit', 'inherit'] });
  ff.on('error', (err) => { console.error('[FFmpeg error]', err); process.exit(1); });
  return ff;
}

// ── Main ──────────────────────────────────────────────────────────────────────
(async () => {
  console.log(`[capture-v2] Launching headless browser — ${TOTAL_FRAMES} frames @ ${FPS}fps (${DURATION}s)`);

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
  await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 });

  console.log(`[capture-v2] Navigating to ${CAPTURE_URL}`);
  await page.goto(CAPTURE_URL, { waitUntil: 'networkidle0', timeout: 60_000 });

  console.log('[capture-v2] Waiting for Live2D model to load …');
  await page.waitForFunction(
    () => window.modelReady === true || window.modelError !== null,
    { timeout: 60_000 }
  );

  const errMsg = await page.evaluate(() => window.modelError);
  if (errMsg) {
    console.error('[capture-v2] Model failed to load:', errMsg);
    await browser.close();
    process.exit(1);
  }
  console.log('[capture-v2] Model ready');

  // Push the full motion schedule into the page so it's available for reference
  await page.evaluate((schedule) => window.setMotionSchedule(schedule), motionSchedule);

  // Enable speaking / lip-sync mode for the entire recording
  await page.evaluate(() => window.setSpeaking(true));
  console.log('[capture-v2] Lip-sync (speaking) mode enabled');

  const ff = startFFmpeg(outputMp4, FPS);

  for (let i = 0; i < TOTAL_FRAMES; i++) {
    // Trigger any motion cues scheduled for this frame
    if (cueMap.has(i)) {
      for (const { group, motionIndex } of cueMap.get(i)) {
        await page.evaluate(
          (g, mi) => window.triggerMotion(g, mi),
          group,
          motionIndex
        );
        console.log(`[capture-v2] Frame ${i}: triggered motion "${group}" index ${motionIndex}`);
      }
    }

    // Advance animation by one frame period and render
    await page.evaluate((ms) => window.advanceFrame(ms), FRAME_MS);

    const pngBuf = await page.screenshot({ type: 'png', omitBackground: false });
    ff.stdin.write(pngBuf);

    if ((i + 1) % FPS === 0 || i === TOTAL_FRAMES - 1) {
      const elapsed = ((i + 1) / FPS).toFixed(1);
      console.log(`[capture-v2] ${i + 1}/${TOTAL_FRAMES} frames (${elapsed}s)`);
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
  console.log(`[capture-v2] Done — ${outputMp4}`);
})().catch((err) => {
  console.error('[capture-v2] Fatal error:', err);
  process.exit(1);
});
