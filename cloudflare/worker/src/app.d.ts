import { D1Database, R2Bucket } from "@cloudflare/workers-types";

declare global {
  namespace App {
    interface Platform {
      env: {
        R2_BUCKET: R2Bucket;
        DB: D1Database;
        WORKER_TOKEN: string;
        CORS_ORIGIN?: string;
      };
    }
  }
}

export {};
