/*
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
*/
import { HTMLAttributes, useState } from 'react';
import styles from '@/styles/DocumentIngestion.module.css';
import { apiFetch } from '@/lib/api';

declare module 'react' {
  interface InputHTMLAttributes<T> extends HTMLAttributes<T> {
    webkitdirectory?: string;
    directory?: string;
  }
}

type Visibility = "private" | "public";

interface DocumentIngestionProps {
  files: FileList | null;
  ingestMessage: string;
  isIngesting: boolean;
  setFiles: (files: FileList | null) => void;
  setIngestMessage: (message: string) => void;
  setIsIngesting: (value: boolean) => void;
  onSuccessfulIngestion?: () => void;
}

export default function DocumentIngestion({
  files,
  ingestMessage,
  isIngesting,
  setFiles,
  setIngestMessage,
  setIsIngesting,
  onSuccessfulIngestion,
}: DocumentIngestionProps) {
  const [visibility, setVisibility] = useState<Visibility>("private");

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFiles(e.target.files);
  };

  const handleIngestSubmit = async (e: { preventDefault: () => void }) => {
    e.preventDefault();
    setIsIngesting(true);
    setIngestMessage("");

    try {
      if (!files || files.length === 0) {
        setIngestMessage("Please select files or specify a directory path.");
        return;
      }

      const formData = new FormData();
      for (let i = 0; i < files.length; i++) {
        formData.append("files", files[i]);
      }
      formData.append("visibility", visibility);

      const res = await apiFetch("/ingest", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();

      if (!res.ok || !data.task_id) {
        setIngestMessage(data.message || "Error during ingestion.");
        return;
      }

      // /ingest returns 200 as soon as the upload lands on disk; the actual
      // chunking + Milvus indexing + Postgres source row happen in a
      // background task. Poll /ingest/status until it terminates so we only
      // fire onSuccessfulIngestion (which triggers the sidebar's /sources
      // refetch) once the row actually exists.
      setIngestMessage("Indexing... this can take a minute for large PDFs.");
      const finalStatus = await pollIngestStatus(data.task_id);

      if (finalStatus === "completed") {
        setIngestMessage("Ingestion complete.");
        if (onSuccessfulIngestion) onSuccessfulIngestion();
      } else if (finalStatus.startsWith("failed")) {
        setIngestMessage(`Ingestion failed: ${finalStatus}`);
      } else {
        setIngestMessage(
          "Ingestion is taking longer than expected. The source will appear once indexing finishes."
        );
        // Still trigger a refresh — by the time the user notices, it may
        // well be ready.
        if (onSuccessfulIngestion) onSuccessfulIngestion();
      }
    } catch (error) {
      console.error("Error during ingestion:", error);
      setIngestMessage("Error during ingestion. Please check the console for details.");
    } finally {
      setIsIngesting(false);
    }
  };

  // Poll /ingest/status/{task_id}. Returns the final status string, or
  // the last status seen if we time out. Backoff stays gentle (every 2s,
  // up to 5 minutes) because chunk+embed+Milvus-insert for a multi-MB PDF
  // can take a minute or more on CPU-only embedding pods.
  const pollIngestStatus = async (taskId: string): Promise<string> => {
    const POLL_INTERVAL_MS = 2000;
    const MAX_ATTEMPTS = 150;  // 5 minutes
    let lastStatus = "queued";
    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      try {
        const r = await apiFetch(`/ingest/status/${taskId}`);
        if (!r.ok) continue;  // transient 5xx — keep trying
        const body = await r.json();
        lastStatus = body.status || lastStatus;
        if (lastStatus === "completed" || lastStatus.startsWith("failed")) {
          return lastStatus;
        }
      } catch (err) {
        // Network blip — keep polling. The background task is independent
        // of this request, so transient failures aren't fatal.
        console.warn("ingest status poll failed:", err);
      }
    }
    return lastStatus;
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      setFiles(e.dataTransfer.files);
    }
  };

  return (
    <div className={styles.section}>
      <h1>Document Ingestion</h1>
      <form onSubmit={handleIngestSubmit} className={styles.ingestForm}>
        <div
          className={styles.uploadSection}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
        >
          <label htmlFor="file-upload" className={styles.customFileLabel}>
            Choose Files
          </label>
          <input
            id="file-upload"
            type="file"
            multiple
            onChange={handleFileChange}
            disabled={isIngesting}
            className={styles.fileInput}
          />
          <span className={styles.fileName}>
            {files && files.length > 0 ? Array.from(files).map(f => f.name).join(', ') : "No file chosen"}
          </span>
          <p className={styles.helpText}>
            Select files or drag and drop them here
          </p>
        </div>

        <fieldset className={styles.visibilityFieldset}>
          <legend>Visibility</legend>
          <label className={styles.visibilityOption}>
            <input
              type="radio"
              name="visibility"
              value="private"
              checked={visibility === "private"}
              onChange={() => setVisibility("private")}
              disabled={isIngesting}
            />
            <span>
              <strong>Private</strong>
              <span className={styles.visibilityHelp}>
                Only you can search these documents
              </span>
            </span>
          </label>
          <label className={styles.visibilityOption}>
            <input
              type="radio"
              name="visibility"
              value="public"
              checked={visibility === "public"}
              onChange={() => setVisibility("public")}
              disabled={isIngesting}
            />
            <span>
              <strong>Public</strong>
              <span className={styles.visibilityHelp}>
                Everyone can search these documents
              </span>
            </span>
          </label>
        </fieldset>

        <button
          type="submit"
          disabled={isIngesting || !files}
          className={styles.ingestButton}
        >
          {isIngesting ? "Ingesting..." : "Ingest Documents"}
        </button>
      </form>

      {ingestMessage && (
        <div className={styles.messageContainer}>
          <p>{ingestMessage}</p>
        </div>
      )}
    </div>
  );
}
