import { describe, it, expect } from "vitest";
import { FakePool } from "./helpers.js";
import { claimExportJob, completeJob, failJob } from "../src/export-worker/jobqueue.js";

describe("export-worker/jobqueue", () => {
  it("claimExportJob claims a queued export job and returns id+payload", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "job-1", payload: { export_id: "exp-1" } }] });
    const claimed = await claimExportJob(pool as any, "exportw-1");
    expect(claimed).toEqual({ id: "job-1", payload: { export_id: "exp-1" } });
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE jobs SET status='running'/i);
    expect(sql).toMatch(/attempts=attempts\+1/);
    expect(sql).toMatch(/status='queued' AND run_after<=now\(\) AND type='export'/i);
    expect(sql).toMatch(/FOR UPDATE SKIP LOCKED/);
    expect(sql).toMatch(/RETURNING id, payload/);
    expect(params).toEqual(["exportw-1"]);
  });

  it("claimExportJob returns null when no job is available", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [] });
    expect(await claimExportJob(pool as any, "exportw-1")).toBeNull();
  });

  it("claimExportJob JSON-parses a string payload defensively", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ id: "job-2", payload: '{"export_id":"exp-2"}' }] });
    const claimed = await claimExportJob(pool as any, "exportw-1");
    expect(claimed).toEqual({ id: "job-2", payload: { export_id: "exp-2" } });
  });

  it("completeJob marks the job done", async () => {
    const pool = new FakePool();
    await completeJob(pool as any, "job-1");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE jobs SET status='done'/i);
    expect(params).toEqual(["job-1"]);
  });

  it("failJob(dead=true) marks the job failed and returns the terminal status", async () => {
    const pool = new FakePool();
    pool.enqueue({ rows: [{ status: "failed" }] });
    const status = await failJob(pool as any, "job-1", "RenderTimeout", true);
    expect(status).toBe("failed");
    const { sql, params } = pool.calls[0];
    expect(sql).toMatch(/UPDATE jobs SET status='failed'/i);
    expect(params).toEqual(["RenderTimeout", "job-1"]);
  });
});
