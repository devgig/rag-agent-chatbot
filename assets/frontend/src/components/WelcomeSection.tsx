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
import styles from "@/styles/WelcomeSection.module.css";

export default function WelcomeSection() {
  return (
    <div className={styles.welcomeContainer}>
      <div className={styles.welcomeMessage}>
        Hello! Send a message to Spark Chat.
      </div>
      <div className={styles.agentCards}>
        <div className={`${styles.agentCard} ${styles.animate1}`}>
          <div className={styles.agentIcon}>
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" width="24" height="24">
              <circle cx="11" cy="11" r="8"/>
              <path d="m21 21-4.35-4.35"/>
            </svg>
          </div>
          <h3 className={styles.agentTitle}>Search Documents</h3>
          <p className={styles.agentSubtitle}>RAG Agent</p>
        </div>
      </div>
    </div>
  );
}
