import { useEffect } from 'react';
import { claudeRequest, type ClaudeContent } from './api';
import type { Dispatch, SetStateAction } from 'react';

export function useKnowledgeDeliveryPolling(
  batchId: string | undefined,
  content: ClaudeContent | null,
  setContent: Dispatch<SetStateAction<ClaudeContent | null>>,
  setError: Dispatch<SetStateAction<string | null>>,
) {
  const pending = content?.documents.filter(item => ['delivery_queued', 'delivery_running', 'delivery_reconciling'].includes(item.status)).map(item => item.record_id).sort().join(',') ?? '';
  useEffect(() => {
    if (!batchId || !pending) return;
    let disposed = false;
    const refresh = async () => {
      for (const id of pending.split(',')) {
        try {
          const job = await claudeRequest<{ status: string; target_id?: string | null; error_message?: string | null }>(`imports/${batchId}/documents/${id}/deliver/status`);
          if (disposed) return;
          const status = ({ queued: 'delivery_queued', submitting: 'delivery_running', reconciling: 'delivery_reconciling', unknown: 'delivery_unknown', failed: 'delivery_rejected', delivered: 'delivered' } as Record<string, string>)[job.status];
          if (!status) throw new Error(`未知投递状态：${job.status}`);
          setContent(current => current && ({ ...current, documents: current.documents.map(item => item.record_id === id ? { ...item, status, target_id: job.target_id ?? item.target_id } : item) }));
          if (job.status === 'failed' || job.status === 'unknown') setError(job.error_message || 'Knowledge 投递未完成，请检查状态。');
        } catch (error) {
          if (!disposed) setError(error instanceof Error ? error.message : String(error));
        }
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [batchId, pending, setContent, setError]);
}
