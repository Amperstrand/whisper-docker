import type { PageLoad } from "./$types";
import type { Job, Segment, Analysis } from "$lib/types";

export const load: PageLoad = async ({ params, fetch }) => {
  const [jobRes, resultRes] = await Promise.all([
    fetch(`/api/jobs/${params.id}`),
    fetch(`/api/jobs/${params.id}/result`),
  ]);

  const jobData = jobRes.ok ? await jobRes.json<{ job: Job }>() : null;
  const resultData = resultRes.ok ? await resultRes.json<{ segments: Segment[] | null; analysis: Analysis | null }>() : null;

  const job = jobData?.job ?? null;
  const segments = resultData?.segments ?? null;
  const analysis = resultData?.analysis ?? null;

  return {
    job,
    segments,
    analysis,
    audioUrl: null,
  };
};
