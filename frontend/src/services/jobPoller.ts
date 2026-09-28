/**
 * jobPoller.ts — frontend half of the v3.18.3 background-job loop.
 *
 * Four backend endpoints now return 202 + `{job_id, status_url}` instead
 * of blocking on long-running work:
 *   - POST /api/recordings/sessions/{id}/finalize (always-on stop)
 *   - GET  /api/digests (cache-miss + force=true paths)
 *   - POST /api/sessions/{id}/tts/summary (cache-miss)
 *   - POST /api/sessions/{id}/tts/podcast (cache-miss)
 *
 * Callers use `pollJob(job_id)` to wait for completion with exponential
 * backoff (2s -> 4s -> 8s -> 16s -> capped at 30s). The promise resolves
 * with the worker's return value on `status="completed"` and rejects on
 * `"failed"` or 404 (job evicted from arq's result window).
 */

import { config } from "../config";

export type JobStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "not_found";

export interface JobStatusResponse {
  job_id: string;
  status: JobStatus;
  result: unknown;
  error: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface PollJobOptions {
  /** Initial poll interval; doubled each tick until capped. Default 2000ms. */
  intervalMs?: number;
  /** Max poll interval (cap for exponential backoff). Default 30000ms. */
  maxIntervalMs?: number;
  /** Overall timeout — reject if not finished by then. Default 10 minutes. */
  maxMs?: number;
  /** Fired on each status snapshot. Caller can drive a progress UI. */
  onProgress?: (status: JobStatusResponse) => void;
  /** Auth headers / cookies — passed straight through to fetch. */
  headers?: Record<string, string>;
  /** Optional AbortSignal — cancel the poll loop early. */
  signal?: AbortSignal;
}

export class JobPollError extends Error {
  constructor(
    message: string,
    public readonly jobId: string,
    public readonly status?: JobStatus,
    public readonly originalError?: string | null,
  ) {
    super(message);
    this.name = "JobPollError";
  }
}

/**
 * Poll an arq job until it finishes.
 *
 * @returns the worker's `result` payload on completion.
 * @throws JobPollError on `status="failed"`, 404 (job evicted), abort,
 *         or overall-timeout.
 */
export async function pollJob<T = unknown>(
  jobId: string,
  opts: PollJobOptions = {},
): Promise<T> {
  const {
    intervalMs = 2000,
    maxIntervalMs = 30000,
    maxMs = 10 * 60 * 1000,
    onProgress,
    headers = {},
    signal,
  } = opts;

  const url = `${config.apiBaseUrl}/api/jobs/${encodeURIComponent(jobId)}`;
  const deadline = Date.now() + maxMs;
  let nextInterval = intervalMs;

  while (true) {
    if (signal?.aborted) {
      throw new JobPollError("Job poll aborted by caller.", jobId);
    }
    if (Date.now() > deadline) {
      throw new JobPollError(
        `Job ${jobId} did not finish within ${maxMs}ms.`,
        jobId,
      );
    }

    let resp: Response;
    try {
      resp = await fetch(url, {
        method: "GET",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...headers,
        },
        signal,
      });
    } catch (err) {
      // Network blip: log + retry on the same backoff schedule.
      console.warn("[jobPoller] fetch failed, retrying:", err);
      await sleep(nextInterval, signal);
      nextInterval = Math.min(nextInterval * 2, maxIntervalMs);
      continue;
    }

    if (resp.status === 404) {
      throw new JobPollError(
        `Job ${jobId} not found (likely evicted from arq).`,
        jobId,
        "not_found",
      );
    }

    if (!resp.ok) {
      // Treat 5xx as transient and retry; surface other 4xx immediately.
      if (resp.status >= 500) {
        console.warn(
          `[jobPoller] ${resp.status} from /api/jobs/${jobId}, retrying`,
        );
        await sleep(nextInterval, signal);
        nextInterval = Math.min(nextInterval * 2, maxIntervalMs);
        continue;
      }
      throw new JobPollError(
        `Job ${jobId} status request failed: ${resp.status}.`,
        jobId,
      );
    }

    const payload = (await resp.json()) as JobStatusResponse;
    onProgress?.(payload);

    if (payload.status === "completed") {
      return payload.result as T;
    }
    if (payload.status === "failed") {
      throw new JobPollError(
        payload.error || `Job ${jobId} failed.`,
        jobId,
        "failed",
        payload.error,
      );
    }
    if (payload.status === "not_found") {
      throw new JobPollError(
        `Job ${jobId} not found.`,
        jobId,
        "not_found",
      );
    }

    // pending | running — wait and try again.
    await sleep(nextInterval, signal);
    nextInterval = Math.min(nextInterval * 2, maxIntervalMs);
  }
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("aborted"));
      return;
    }
    const t = window.setTimeout(() => {
      resolve();
    }, ms);
    signal?.addEventListener(
      "abort",
      () => {
        window.clearTimeout(t);
        reject(new Error("aborted"));
      },
      { once: true },
    );
  });
}
