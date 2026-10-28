import { ownerFetch } from './ownerSession';
import type { PackKind, PackExportRequest } from './api';

export type GrantMode = 'live_scope' | 'approved_pack';
export type GatewayOperation = 'search_memory' | 'get_memory' | 'get_evidence' | 'build_context' | 'get_pack_info' | 'read_memory_pack';
export interface GrantInput {
  subject: string; purpose: string; mode: GrantMode; project_ids: string[]; kinds: PackKind[];
  operations: GatewayOperation[]; expires_in_hours: number; token_budget: number;
  evidence_mode: 'none' | 'id' | 'snippet'; requests_per_minute: number;
  confirm_transfer: boolean; confirm_future_allow: boolean; draft_id?: string;
}
export interface Grant extends GrantInput {
  id: string; last_used_at?: string | null; version: number; fingerprint: string; created_at: string; expires_at: string;
  status: 'active' | 'revoked' | 'expired' | 'consumed' | 'stale'; pack_id: string | null; token?: string;
}
export interface FixedDraft { draft_id: string; project_ids: string[]; kinds: PackKind[]; token_budget: number; evidence_mode: 'none' | 'id'; included_count: number; }
export interface GatewayStatus { paused: boolean; gateway_url: string; everos_online: boolean; gateway_online?: boolean; }
export interface GatewayGrantPage { items: Grant[]; total: number; limit: number; offset: number; }
export interface AuditRow { id: string; operation: string; transport: string; created_at: string; outcome: string; reason: string | null; metadata_json?: string; }
async function request<T>(path: string, method = 'GET', body?: unknown, requestKey?: string): Promise<T> {
  const response = await ownerFetch(`/api/v1${path}`, { method, headers: { 'Content-Type': 'application/json', ...(requestKey ? { 'Idempotency-Key': requestKey } : {}) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.detail?.message ?? `HTTP ${response.status}`);
  return result.data as T;
}
export async function listGrants(bucket: 'all' | 'active' | 'history', limit: number, offset: number): Promise<GatewayGrantPage> {
  const response = await request<unknown>(`/grants?bucket=${bucket}&limit=${limit}&offset=${offset}`);
  // Older Vault processes return a bare array and ignore pagination parameters.
  if (Array.isArray(response)) {
    const all = response as Grant[];
    const matching = bucket === 'all' ? all : all.filter(grant => bucket === 'active'
      ? grant.status === 'active' : grant.status !== 'active');
    return { items: matching.slice(offset, offset + limit), total: matching.length, limit, offset };
  }
  if (response && typeof response === 'object') {
    const page = response as Partial<GatewayGrantPage>;
    if (Array.isArray(page.items) && Number.isFinite(page.total)) {
      return { items: page.items, total: page.total!, limit: page.limit ?? limit, offset: page.offset ?? offset };
    }
  }
  throw new Error('授权列表响应格式无效，请刷新 Vault 服务后重试。');
}
export const gatewayStatus = () => request<GatewayStatus>('/gateway/status');
export const pauseGateway = (paused: boolean) => request<{ paused: boolean }>(`/gateway/${paused ? 'pause' : 'resume'}`, 'POST');
export const createGrant = (body: GrantInput, requestKey: string) => request<Grant>('/grants', 'POST', body, requestKey);
export const revokeGrant = (id: string) => request(`/grants/${encodeURIComponent(id)}`, 'DELETE');
export const deleteGrant = (id: string) => request(`/grants/${encodeURIComponent(id)}/permanent`, 'DELETE');
export const rotateGrant = (id: string) => request<Grant>(`/grants/${encodeURIComponent(id)}/rotate`, 'POST');
export const getGrantAudit = (id: string) => request<AuditRow[]>(`/grants/${encodeURIComponent(id)}/audit`);
export async function createPackDraft(body: PackExportRequest): Promise<FixedDraft> {
  const result = await request<{ draft_id: string; included_count: number }>('/grants/pack-drafts', 'POST', body);
  if (body.evidence_mode !== 'none' && body.evidence_mode !== 'id') throw new Error('Unsupported evidence mode');
  return { ...result, project_ids: body.namespaces, kinds: body.kinds, token_budget: body.token_budget, evidence_mode: body.evidence_mode };
}
export async function callGateway(url: string, token: string, operation: GatewayOperation, body: Record<string, unknown>, signal?: AbortSignal) {
  const paths: Record<GatewayOperation, string> = { search_memory: '/memories/search', get_memory: '/memories/get', get_evidence: '/evidence/get', build_context: '/context/build', get_pack_info: `/packs/${encodeURIComponent(String(body.pack_id))}`, read_memory_pack: `/packs/${encodeURIComponent(String(body.pack_id))}/deliver` };
  const isGet = operation === 'get_pack_info';
  const response = await fetch(`${url}/api/v1/external${paths[operation]}`, { method: isGet ? 'GET' : 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, credentials: 'omit', signal, ...(isGet ? {} : { body: JSON.stringify(body) }) });
  const raw = await response.text();
  let result: unknown; try { result = JSON.parse(raw); } catch { result = raw; }
  return { ok: response.ok, status: response.status, receipt: response.headers.get('X-Vault-Receipt'), result };
}
