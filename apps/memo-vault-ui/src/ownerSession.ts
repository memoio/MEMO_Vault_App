// Owner credentials remain in memory; the session cookie is HTTP-only.
let csrfToken = "";
export function setOwnerCsrf(token: string) { csrfToken = token; }
export async function ownerFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes((init.method ?? 'GET').toUpperCase())) headers.set('X-Vault-CSRF', csrfToken);
  const response = await fetch(input, { ...init, headers, credentials: 'same-origin' });
  if (response.status === 401) window.dispatchEvent(new Event('vault-session-expired'));
  return response;
}
