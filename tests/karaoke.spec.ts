import { test, expect } from "@playwright/test";
import path from "path";

const AUDIO_FIXTURE = path.join(__dirname, "..", "tests", "fixtures", "jfk.wav");
const POLL_INTERVAL = 3_000;
const MAX_WAIT = 240_000;

test("karaoke player highlights words during playback", async ({ page }) => {
  await page.goto("/", { waitUntil: "networkidle" });

  const fileInput = page.locator("#file-input");
  await expect(fileInput).toBeAttached();

  await fileInput.setInputFiles(AUDIO_FIXTURE);

  await expect(page.locator(".drop-zone")).not.toBeVisible();
  await expect(page.locator(".audio-container audio")).toBeAttached({ timeout: 5_000 });

  const transcribeBtn = page.locator("button.btn-primary:has-text('Transcribe')");
  await expect(transcribeBtn).toBeVisible();
  await transcribeBtn.click();

  await expect(page.locator(".status-badge")).toBeVisible({ timeout: 5_000 });

  let elapsed = 0;
  while (elapsed < MAX_WAIT) {
    const badge = page.locator(".status-badge");
    const text = await badge.textContent();
    if (text?.includes("Transcribing") || text?.includes("Completed")) break;
    await page.waitForTimeout(POLL_INTERVAL);
    elapsed += POLL_INTERVAL;
  }

  await expect(page.locator(".word").first()).toBeVisible({ timeout: MAX_WAIT });

  await page.evaluate(async () => {
    const audio = document.querySelector("audio") as HTMLAudioElement;
    if (!audio) return;
    audio.muted = true;
    audio.currentTime = 4.5;
    await audio.play();
  });

  await page.waitForTimeout(2_000);

  await expect(page.locator(".word.active")).toBeVisible({ timeout: 5_000 });

  const pastCount = await page.locator(".word.past").count();
  const futureCount = await page.locator(".word.future").count();
  expect(pastCount).toBeGreaterThan(0);
  expect(futureCount).toBeGreaterThan(0);

  await page.screenshot({
    path: path.join(__dirname, "..", "test-results", "karaoke-highlight.png"),
    fullPage: false,
  });

  await page.locator(".transcript-scroll").screenshot({
    path: path.join(__dirname, "..", "test-results", "karaoke-transcript.png"),
  });

  const activeStyle = await page.locator(".word.active").first().evaluate((el) => {
    const computed = window.getComputedStyle(el);
    return {
      color: computed.color,
      fontWeight: computed.fontWeight,
      opacity: computed.opacity,
    };
  });

  expect(parseFloat(activeStyle.opacity)).toBeGreaterThan(0.8);
});
