export type RoleName =
  | 'root'
  | 'platform_owner'
  | 'org_owner'
  | 'security_admin'
  | 'developer'
  | 'intern'
  | 'auditor';

export type HealthResponse = {
  ok: boolean;
  bootstrapped: boolean;
};

export type AuditVerification = {
  valid: boolean;
  event_count: number;
  broken_index: number | null;
  expected_hash: string | null;
  actual_hash: string | null;
};

export type Actor = {
  user_id: string;
  organization_id: string | null;
  role: RoleName;
  platform_role: RoleName | null;
  allowed_tools: string[] | null;
  usage_policy: UsagePolicy | null;
  model_name: string | null;
  model_provider: ModelProvider | null;
  model_base_url: string | null;
  model_requires_api_key: boolean | null;
  network_policy: NetworkPolicy | null;
};

export type ModelSetting = {
  organization_id: string | null;
  model: string | null;
  catalog_entry: ModelCatalogEntry | null;
};

export type ModelCatalogEntry = {
  id: string;
  label: string;
  provider: ModelProvider;
  base_url: string | null;
  requires_api_key: boolean;
  enabled: boolean;
  free: boolean;
  notes: string;
};

export type Organization = {
  id: string;
  name: string;
  domains: string[];
  created_at: string;
};

export type Member = {
  user_id: string;
  organization_id: string;
  role: RoleName;
  created_at: string;
};

export type AuditEvent = {
  id: string;
  timestamp: string;
  event_type: string;
  actor: Record<string, unknown>;
  details: Record<string, unknown>;
  previous_hash: string | null;
  event_hash: string | null;
};

export type BootstrapPayload = {
  organization_name: string;
  root_user_id: string;
  root_display_name: string;
  root_password?: string | null;
  root_token?: string | null;
  root_token_expires_at?: string | null;
};

export type LoginResponse = {
  token: string;
  token_expires_at: string;
  actor: Actor;
};

export type MemberPayload = {
  user_id: string;
  display_name?: string;
  role: RoleName;
  password?: string | null;
  token?: string | null;
  token_expires_at?: string | null;
};

export type RotateTokenPayload = {
  token: string;
  token_expires_at?: string | null;
};

export type SetupCheck = {
  key: string;
  label: string;
  ok: boolean;
  severity: 'critical' | 'warning';
};

export type SetupStatus = {
  ok: boolean;
  checks: SetupCheck[];
  production: boolean;
  configured_model_base_url: string | null;
  model_api_key_present: boolean;
  microsoft_sign_in_enabled: boolean;
  control_token_present: boolean;
  audit: AuditVerification;
};

export type RoleDashboard = {
  role: RoleName;
  organization_id: string | null;
  capabilities: string[];
  next_actions: string[];
  usage_policy: UsagePolicy | null;
  allowed_tools: string[] | null;
  active_sessions: number;
  visible_sessions: number;
  audit_events_visible: number;
  audit_chain_valid: boolean;
  pending_escalations: number;
};

export type AuditExport = {
  organization_id: string | null;
  generated_at: string;
  audit: AuditVerification;
  events: AuditEvent[];
  attestations: Record<string, unknown>[];
};

export type NetworkPolicy = {
  allow_network: boolean;
  allowed_domains: string[];
  blocked_domains: string[];
  require_proxy: boolean;
  proxy_url: string | null;
};

export type IntegrationRecord = {
  id: string;
  provider:
    | 'github'
    | 'gitlab'
    | 'jira'
    | 'slack'
    | 'siem'
    | 'scim'
    | 'saml'
    | 'mcp'
    | 'custom';
  name: string;
  organization_id: string | null;
  enabled: boolean;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ProjectProfile = {
  id: string;
  organization_id: string;
  repository: string;
  default_branch: string;
  verification_commands: string[];
  allowed_domains: string[];
  issue_tracker_url: string | null;
  secret_env_vars: string[];
  created_at: string;
  updated_at: string;
};

export type CliTokenSummary = {
  id: string;
  name: string;
  token_expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

export type CliTokenResponse = CliTokenSummary & {
  token: string;
};

export type ApprovalRequest = {
  id: string;
  organization_id: string | null;
  requester_user_id: string;
  requester_role: RoleName;
  request_type: 'tool' | 'network' | 'model' | 'workspace' | 'integration' | 'other';
  target: string;
  reason: string;
  status: 'pending' | 'approved' | 'rejected';
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
};

export type ProductionReadiness = {
  score: number;
  ready: boolean;
  gaps: string[];
  checks: Record<string, boolean>;
};

export type ModelProvider = 'openai_compatible' | 'anthropic' | 'claude_code';

export type ModelConnectivityPayload = {
  provider: ModelProvider;
  base_url: string;
  model: string;
  api_key?: string | null;
  timeout_seconds?: number;
};

export type ModelConnectivityResult = {
  ok: boolean;
  provider: ModelProvider;
  message: string;
  status_code: number | null;
  latency_ms: number | null;
};

export type UsagePolicy = {
  enabled: boolean;
  requests_per_minute: number | null;
  requests_per_hour: number | null;
  requests_per_day: number | null;
  org_requests_per_day: number | null;
  max_turns_per_session: number | null;
  max_session_minutes: number | null;
  max_context_tokens: number | null;
};

export type UsageSummary = {
  last_day: Record<string, number>;
  last_hour: Record<string, number>;
  event_count: number;
};

export type AgentSession = {
  id: string;
  user_id: string;
  organization_id: string | null;
  role: RoleName;
  cwd: string;
  model: string;
  status: 'active' | 'ended' | 'revoked';
  started_at: string;
  last_heartbeat_at: string;
  ended_at: string | null;
  turn_count: number;
  last_message_preview: string;
};

const API_BASE = import.meta.env.VITE_CONTROL_PLANE_URL ?? '';

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit & { token?: string | null; query?: Record<string, string | number | null | undefined> } = {}
): Promise<T> {
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  Object.entries(options.query ?? {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') {
      url.searchParams.set(key, String(value));
    }
  });

  const headers = new Headers(options.headers);
  if (!headers.has('Content-Type') && options.body) {
    headers.set('Content-Type', 'application/json');
  }
  if (options.token) {
    headers.set('Authorization', `Bearer ${options.token}`);
  }

  const response = await fetch(url, {
    ...options,
    headers,
    cache: 'no-store',
    credentials: 'same-origin'
  });

  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body);
    } catch {
      message = await response.text();
    }
    throw new ApiError(response.status, message || 'Request failed');
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const controlPlaneApi = {
  health: () => request<HealthResponse>('/health'),
  ready: (token: string | null) => request<{ ok: boolean; audit: AuditVerification }>('/ready', { token }),
  setupStatus: (token?: string | null) =>
    request<SetupStatus>('/setup/status', token === undefined ? {} : { token }),
  modelCheck: (token: string | null, payload: ModelConnectivityPayload) =>
    request<ModelConnectivityResult>('/setup/model-check', {
      token,
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  bootstrap: (payload: BootstrapPayload, bootstrapSecret: string) =>
    request<{ organization: Organization; root_user: { id: string }; root_token?: string | null }>('/bootstrap', {
      method: 'POST',
      ...(bootstrapSecret ? { headers: { 'X-Bootstrap-Secret': bootstrapSecret } } : {}),
      body: JSON.stringify(payload)
    }),
  login: (userId: string, password: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ user_id: userId, password })
    }),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  microsoftStartUrl: () => `${API_BASE}/auth/microsoft/start`,
  issueCliToken: (token: string | null, tokenExpiresAt?: string | null, name = 'CLI token') =>
    request<CliTokenResponse>('/cli/token', {
      token,
      method: 'POST',
      body: JSON.stringify({ name, token_expires_at: tokenExpiresAt ?? null })
    }),
  cliTokens: (token: string | null) => request<CliTokenSummary[]>('/cli/tokens', { token }),
  revokeCliToken: (token: string | null, tokenId: string) =>
    request<CliTokenSummary>(`/cli/tokens/${tokenId}`, {
      token,
      method: 'DELETE'
    }),
  me: (token: string | null, organizationId?: string | null) =>
    request<Actor>('/me', { token, query: { organization_id: organizationId } }),
  organizations: (token: string | null) =>
    request<{ organizations: Organization[] }>('/organizations', { token }),
  createOrganization: (token: string | null, name: string, domains: string[] = []) =>
    request<Organization>('/organizations', {
      token,
      method: 'POST',
      body: JSON.stringify({ name, domains })
    }),
  members: (token: string | null, organizationId: string) =>
    request<{ members: Member[] }>(`/organizations/${organizationId}/members`, { token }),
  upsertMember: (token: string | null, organizationId: string, payload: MemberPayload) =>
    request<Member>(`/organizations/${organizationId}/members`, {
      token,
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  removeMember: (token: string | null, organizationId: string, userId: string) =>
    request<{ ok: boolean }>(`/organizations/${organizationId}/members/${userId}`, {
      token,
      method: 'DELETE'
    }),
  rotateToken: (token: string | null, organizationId: string, userId: string, payload: RotateTokenPayload) =>
    request<{ user_id: string }>(`/organizations/${organizationId}/members/${userId}/rotate-token`, {
      token,
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  roleDashboard: (token: string | null, organizationId?: string | null) =>
    request<RoleDashboard>('/role-dashboard', { token, query: { organization_id: organizationId } }),
  orgRolePolicies: (token: string | null, organizationId: string) =>
    request<{ role_policies: Record<string, string[] | null> }>(`/organizations/${organizationId}/policies/roles`, {
      token
    }),
  updateOrgRolePolicy: (token: string | null, organizationId: string, role: RoleName, allowedTools: string[] | null) =>
    request<{ organization_id: string; role: RoleName; allowed_tools: string[] | null }>(
      `/organizations/${organizationId}/policies/roles`,
      {
        token,
        method: 'PUT',
        body: JSON.stringify({ role, allowed_tools: allowedTools })
      }
    ),
  rolePolicies: (token: string | null) =>
    request<{ role_policies: Record<string, string[] | null> }>('/policies/roles', { token }),
  updateRolePolicy: (token: string | null, role: RoleName, allowedTools: string[] | null) =>
    request<{ role: RoleName; allowed_tools: string[] | null }>(`/policies/roles/${role}`, {
      token,
      method: 'PUT',
      body: JSON.stringify({ allowed_tools: allowedTools })
    }),
  usagePolicies: (token: string | null) =>
    request<{ usage_policies: Record<string, UsagePolicy> }>('/usage/policies', { token }),
  updateUsagePolicy: (token: string | null, role: RoleName, policy: UsagePolicy) =>
    request<{ role: RoleName; policy: UsagePolicy }>(`/usage/policies/${role}`, {
      token,
      method: 'PUT',
      body: JSON.stringify(policy)
    }),
  usageSummary: (token: string | null, organizationId?: string | null) =>
    request<UsageSummary>('/usage/summary', {
      token,
      query: { organization_id: organizationId }
    }),
  modelSetting: (token: string | null, organizationId?: string | null) =>
    request<ModelSetting>('/settings/model', {
      token,
      query: { organization_id: organizationId }
    }),
  modelCatalog: (token: string | null) =>
    request<{ models: ModelCatalogEntry[] }>('/settings/model/catalog', { token }),
  upsertModelCatalogEntry: (token: string | null, entry: ModelCatalogEntry) =>
    request<ModelCatalogEntry>('/settings/model/catalog', {
      token,
      method: 'PUT',
      body: JSON.stringify(entry)
    }),
  setModelSetting: (token: string | null, model: string, organizationId?: string | null) =>
    request<ModelSetting>('/settings/model', {
      token,
      method: 'PUT',
      body: JSON.stringify({ model, organization_id: organizationId ?? null })
    }),
  sessions: (token: string | null, organizationId?: string | null, status?: string | null, limit = 100) =>
    request<{ sessions: AgentSession[] }>('/sessions', {
      token,
      query: { organization_id: organizationId, status, limit }
    }),
  revokeSession: (token: string | null, sessionId: string) =>
    request<AgentSession>(`/sessions/${sessionId}/revoke`, {
      token,
      method: 'POST'
    }),
  auditEvents: (token: string | null, organizationId?: string | null, limit = 100) =>
    request<{ events: AuditEvent[] }>('/audit/events', {
      token,
      query: { organization_id: organizationId, limit }
    }),
  auditVerify: (token: string | null) => request<AuditVerification>('/audit/verify', { token }),
  requestInternEscalation: (
    token: string | null,
    payload: { organization_id?: string | null; reason: string; requested_tool?: string | null; session_id?: string | null }
  ) =>
    request<AuditEvent>('/intern/escalations', {
      token,
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  auditExport: (token: string | null, organizationId?: string | null, limit = 500) =>
    request<AuditExport>('/audit/export', {
      token,
      query: { organization_id: organizationId, limit }
    }),
  networkPolicy: (token: string | null, organizationId?: string | null) =>
    request<{ organization_id: string; policy: NetworkPolicy }>('/network/policy', {
      token,
      query: { organization_id: organizationId }
    }),
  updateNetworkPolicy: (token: string | null, organizationId: string | null, policy: NetworkPolicy) =>
    request<{ organization_id: string; policy: NetworkPolicy }>('/network/policy', {
      token,
      method: 'PUT',
      body: JSON.stringify({ ...policy, organization_id: organizationId })
    }),
  integrations: (token: string | null, organizationId?: string | null) =>
    request<{ integrations: IntegrationRecord[] }>('/integrations', {
      token,
      query: { organization_id: organizationId }
    }),
  upsertIntegration: (
    token: string | null,
    payload: Omit<IntegrationRecord, 'created_at' | 'updated_at'>
  ) =>
    request<IntegrationRecord>('/integrations', {
      token,
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  projectProfiles: (token: string | null, organizationId?: string | null, repository?: string | null) =>
    request<{ project_profiles: ProjectProfile[] }>('/project-profiles', {
      token,
      query: { organization_id: organizationId, repository }
    }),
  upsertProjectProfile: (
    token: string | null,
    payload: Omit<ProjectProfile, 'created_at' | 'updated_at'>
  ) =>
    request<ProjectProfile>('/project-profiles', {
      token,
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  approvals: (token: string | null, organizationId?: string | null, status?: string | null) =>
    request<{ approvals: ApprovalRequest[] }>('/approvals', {
      token,
      query: { organization_id: organizationId, status }
    }),
  createApproval: (
    token: string | null,
    payload: Pick<ApprovalRequest, 'request_type' | 'target' | 'reason'> & { organization_id?: string | null }
  ) =>
    request<ApprovalRequest>('/approvals', {
      token,
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  decideApproval: (token: string | null, approvalId: string, status: 'approved' | 'rejected') =>
    request<ApprovalRequest>(`/approvals/${approvalId}/decision`, {
      token,
      method: 'POST',
      body: JSON.stringify({ status })
    }),
  productionReadiness: (token: string | null) =>
    request<ProductionReadiness>('/production/readiness', { token })
};
