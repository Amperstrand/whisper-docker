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
  options: string | null;
}

export interface CreateJobResponse {
  id: string;
  status: string;
  original_filename: string;
  file_size: number;
  options?: string | null;
}

export interface JobResponse {
  job: Job;
}

export interface TranscriptResult {
  id: string;
  status: string;
  transcript: string | null;
  segments: unknown[] | null;
  analysis?: unknown;
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
  speaker?: string;
  emotion?: { label: string; score: number };
  speech_ratio?: number;
  words?: Word[];
}

export interface Word {
  word: string;
  start: number;
  end: number;
  probability: number;
}

export interface Analysis {
  language?: string;
  language_probability?: number;
  speakers?: string[];
  speaker_turns?: number;
  vad?: { speech_ratio: number; speech_segments: number; total_speech_seconds: number };
  emotions?: Record<string, number>;
  audio_tags?: Array<{ label: string; score: number }>;
  language_id?: { label: string; score: number };
  pipeline_duration?: number;
}
