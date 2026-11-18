import { useEffect, useState } from 'react';
import { claudeRequest } from './api';

interface Inventory {
  batch_id: string; package_id: string; tracks: Record<string, number>;
  agent_events_stored: number; total: number; page: number; page_size: number;
  items: { record_id: string; record_type: string; track: string; status: string;
    target_id?: string | null; name?: string; source_member?: string }[];
  source_files: { name: string; bytes?: number; status: string; container?: string }[];
}
const labels: Record<string, string> = { conversation: '用户对话', agent: 'Agent 轨迹',
  knowledge: 'Knowledge 文件', assertion: '平台记忆声明', other: '其他 UMIF 记录' };

export default function TrackInventory({ batchId }: { batchId: string }) {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    void claudeRequest<Inventory>(`imports/${batchId}/content-inventory?page=1&page_size=30`)
      .then(result => { if (current) { setInventory(result); setError(null); } })
      .catch(reason => { if (current) setError(String(reason)); });
    return () => { current = false; };
  }, [batchId]);
  return <section className="track-inventory"><h3>归档内容轨道</h3>
    {error && <p className="notice error">读取内容清单失败：{error}</p>}
    {!inventory && !error && <p>正在读取 UMIF 清单…</p>}
    {inventory && <><div className="track-counts">{Object.entries(inventory.tracks).map(([track, count]) =>
      <span key={track}><strong>{count}</strong> {labels[track] ?? track}</span>)}</div>
      <p className="subtle">UMIF 中有 {inventory.tracks.agent ?? 0} 条 Agent 轨迹；Vault 独立工具事件表保留 {inventory.agent_events_stored} 条。工具参数和结果不进入用户记忆抽取。</p>
    </>}
  </section>;
}
