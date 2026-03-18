export interface Job {
  id: string;
  status: "pending" | "processing" | "completed" | "failed";
  original_filename: string;
  file_size: number;
  file_type: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  worker_id: string | null;
  error_message: string | null;
}

export interface CreateJobResponse {
  id: string;
  status: string;
  original_filename: string;
  file_size: number;
}

export interface JobResponse {
  job: Job;
}

export interface TranscriptResult {
  id: string;
  status: string;
  transcript: string | null;
  segments: unknown[] | null;
  error?: string | null;
}

export interface SuccessResponse {
  success: boolean;
}

export interface PendingJobsResponse {
  jobs: Job[];
}

export interface Segment {
  start: number;
  end: number;
  text: string;
  words?: Word[];
}

export interface Word {
  word: string;
  start: number;
  end: number;
  probability: number;
}
