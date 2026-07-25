import { acceptHMRUpdate, defineStore } from 'pinia';
import {
  type Actor,
  type AgentSession,
  type AuditEvent,
  type AuditVerification,
  type ApprovalRequest,
  type CliTokenSummary,
  type HealthResponse,
  type IntegrationRecord,
  type Member,
  type MemberPayload,
  type ModelCatalogEntry,
  type ModelConnectivityPayload,
  type ModelConnectivityResult,
  type NetworkPolicy,
  type Organization,
  type ProductionReadiness,
  type ProjectProfile,
  type RoleName,
  type RoleDashboard,
  type SetupStatus,
  type UsagePolicy,
  type UsageSummary,
  controlPlaneApi
} from 'src/api/controlPlane';

const SESSION_TOKEN_KEY = 'nyansa.control-plane.token';
const SELECTED_ORG_KEY = 'nyansa.control-plane.organization';

export const roles: RoleName[] = [
  'root',
  'platform_owner',
  'org_owner',
  'security_admin',
  'developer',
  'intern',
  'auditor'
];

export const toolCatalog = [
  'read_file',
  'list_dir',
  'grep',
  'glob',
  'web_search',
  'web_fetch',
  'todos',
  'todo',
  'memory',
  'repo_map',
  'edit',
  'edit_file',
  'write_file',
  'shell',
  'mcp',
  'subagent'
];

export const useControlPlaneStore = defineStore('control-plane', {
  state: () => ({
    token: sessionStorage.getItem(SESSION_TOKEN_KEY) ?? '',
    selectedOrganizationId: sessionStorage.getItem(SELECTED_ORG_KEY) ?? '',
    health: null as HealthResponse | null,
    ready: null as { ok: boolean; audit: AuditVerification } | null,
    actor: null as Actor | null,
    organizations: [] as Organization[],
    members: [] as Member[],
    rolePolicies: {} as Record<string, string[] | null>,
    usagePolicies: {} as Record<string, UsagePolicy>,
    usageSummary: null as UsageSummary | null,
    modelSetting: null as string | null,
    modelCatalog: [] as ModelCatalogEntry[],
    networkPolicy: null as NetworkPolicy | null,
    integrations: [] as IntegrationRecord[],
    projectProfiles: [] as ProjectProfile[],
    approvals: [] as ApprovalRequest[],
    productionReadiness: null as ProductionReadiness | null,
    agentSessions: [] as AgentSession[],
    auditEvents: [] as AuditEvent[],
    auditVerification: null as AuditVerification | null,
    setupStatus: null as SetupStatus | null,
    modelCheckResult: null as ModelConnectivityResult | null,
    roleDashboard: null as RoleDashboard | null,
    orgRolePolicies: {} as Record<string, string[] | null>,
    auditExport: null as Awaited<ReturnType<typeof controlPlaneApi.auditExport>> | null,
    cliTokens: [] as CliTokenSummary[],
    issuedCliToken: '',
    issuedCliTokenExpiresAt: null as string | null,
    loading: false,
    error: ''
  }),

  getters: {
    isAuthenticated: (state) => Boolean(state.actor),
    activeOrganization: (state) =>
      state.organizations.find((org: Organization) => org.id === state.selectedOrganizationId) ??
      state.organizations[0] ??
      null,
    canManagePlatform: (state) => ['root', 'platform_owner'].includes(state.actor?.role ?? ''),
    canManageMembers: (state) =>
      ['root', 'platform_owner', 'org_owner', 'security_admin'].includes(state.actor?.role ?? ''),
    canViewAudit: (state) =>
      ['root', 'platform_owner', 'org_owner', 'security_admin', 'auditor'].includes(state.actor?.role ?? ''),
    currentAllowedTools: (state) => {
      if (state.actor?.allowed_tools === null) {
        return toolCatalog;
      }
      return state.actor?.allowed_tools ?? [];
    }
  },

  actions: {
    setToken(token: string) {
      this.token = token.trim();
      if (this.token) {
        sessionStorage.setItem(SESSION_TOKEN_KEY, this.token);
      } else {
        sessionStorage.removeItem(SESSION_TOKEN_KEY);
      }
    },

    setOrganization(organizationId: string) {
      this.selectedOrganizationId = organizationId;
      if (organizationId) {
        sessionStorage.setItem(SELECTED_ORG_KEY, organizationId);
      } else {
        sessionStorage.removeItem(SELECTED_ORG_KEY);
      }
    },

    signOut() {
      void controlPlaneApi.logout();
      this.setToken('');
        this.actor = null;
        this.ready = null;
        this.setupStatus = null;
        this.modelCheckResult = null;
        this.organizations = [];
      this.members = [];
      this.rolePolicies = {};
      this.usagePolicies = {};
      this.usageSummary = null;
      this.modelSetting = null;
      this.modelCatalog = [];
      this.networkPolicy = null;
      this.integrations = [];
      this.projectProfiles = [];
      this.approvals = [];
      this.productionReadiness = null;
      this.agentSessions = [];
      this.auditEvents = [];
      this.auditVerification = null;
      this.roleDashboard = null;
      this.orgRolePolicies = {};
      this.auditExport = null;
      this.cliTokens = [];
      this.issuedCliToken = '';
      this.issuedCliTokenExpiresAt = null;
    },

    async bootstrap(payload: {
      organizationName: string;
      rootUserId: string;
      rootDisplayName: string;
      rootPassword: string;
      rootToken?: string;
      bootstrapSecret: string;
    }) {
      this.loading = true;
      this.error = '';
      try {
        await controlPlaneApi.bootstrap(
          {
            organization_name: payload.organizationName,
            root_user_id: payload.rootUserId,
            root_display_name: payload.rootDisplayName,
            root_password: payload.rootPassword,
            root_token: payload.rootToken || null
          },
          payload.bootstrapSecret
        );
        await this.login(payload.rootUserId, payload.rootPassword);
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'Bootstrap failed';
        throw error;
      } finally {
        this.loading = false;
      }
    },

    async login(userId: string, password: string) {
      this.loading = true;
      this.error = '';
      try {
        const result = await controlPlaneApi.login(userId, password);
        this.setToken(result.token);
        this.actor = result.actor;
        await this.refreshAll();
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'Login failed';
        throw error;
      } finally {
        this.loading = false;
      }
    },

    async issueCliToken() {
      const result = await controlPlaneApi.issueCliToken(this.token || null);
      this.issuedCliToken = result.token;
      this.issuedCliTokenExpiresAt = result.token_expires_at ?? null;
      await this.refreshCliTokens();
      return result;
    },

    async refreshCliTokens() {
      if (!this.actor) {
        this.cliTokens = [];
        return;
      }
      this.cliTokens = await controlPlaneApi.cliTokens(this.token || null);
    },

    async revokeCliToken(tokenId: string) {
      await controlPlaneApi.revokeCliToken(this.token || null, tokenId);
      await this.refreshCliTokens();
    },

    async refreshAll() {
      this.loading = true;
      this.error = '';
      try {
        this.health = await controlPlaneApi.health();
        this.setupStatus = await controlPlaneApi.setupStatus(this.token || null);

        this.actor = await controlPlaneApi.me(this.token || null, this.selectedOrganizationId);
        const organizationResult = await controlPlaneApi.organizations(this.token || null);
        this.organizations = organizationResult.organizations;
        if (!this.selectedOrganizationId && this.actor.organization_id) {
          this.setOrganization(this.actor.organization_id);
        }
        const firstOrganization = this.organizations[0];
        if (!this.selectedOrganizationId && firstOrganization) {
          this.setOrganization(firstOrganization.id);
        }

        await Promise.all([
          this.refreshReadiness(),
          this.refreshRoleDashboard(),
          this.refreshMembers(),
          this.refreshPolicies(),
          this.refreshUsage(),
          this.refreshModelCatalog(),
          this.refreshModelSetting(),
          this.refreshProductionGovernance(),
          this.refreshCliTokens(),
          this.refreshSessions(),
          this.refreshAudit()
        ]);
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'Unable to load control plane';
        if (!this.token && this.error.includes('Missing bearer token')) {
          this.error = '';
          this.actor = null;
          return;
        }
        if (this.error.includes('Invalid token') || this.error.includes('Token expired')) {
          this.signOut();
        }
      } finally {
        this.loading = false;
      }
    },

    async refreshReadiness() {
      try {
        this.ready = await controlPlaneApi.ready(this.token || null);
      } catch {
        this.ready = null;
      }
    },

    async checkModel(payload: ModelConnectivityPayload) {
      this.modelCheckResult = await controlPlaneApi.modelCheck(this.token || null, payload);
      return this.modelCheckResult;
    },

    async refreshRoleDashboard() {
      try {
        this.roleDashboard = await controlPlaneApi.roleDashboard(
          this.token || null,
          this.selectedOrganizationId || this.actor?.organization_id
        );
      } catch {
        this.roleDashboard = null;
      }
    },

    async refreshMembers() {
      const orgId = this.selectedOrganizationId || this.actor?.organization_id;
      if (!orgId || !this.canManageMembers) {
        this.members = [];
        return;
      }
      const result = await controlPlaneApi.members(this.token || null, orgId);
      this.members = result.members;
      if (this.canManageMembers) {
        try {
          this.orgRolePolicies = await controlPlaneApi
            .orgRolePolicies(this.token || null, orgId)
            .then((result) => result.role_policies);
        } catch {
          this.orgRolePolicies = {};
        }
      }
    },

    async refreshPolicies() {
      if (!this.canManagePlatform) {
        this.rolePolicies = {};
        return;
      }
      const result = await controlPlaneApi.rolePolicies(this.token || null);
      this.rolePolicies = result.role_policies;
    },

    async refreshUsage() {
      if (!this.canManagePlatform) {
        this.usagePolicies = {};
        this.usageSummary = null;
        return;
      }
      const orgId = this.selectedOrganizationId || this.actor?.organization_id;
      const [policies, summary] = await Promise.all([
        controlPlaneApi.usagePolicies(this.token || null),
        controlPlaneApi.usageSummary(this.token || null, orgId)
      ]);
      this.usagePolicies = policies.usage_policies;
      this.usageSummary = summary;
    },

    async refreshModelSetting() {
      const orgId = this.selectedOrganizationId || this.actor?.organization_id;
      if (!orgId) {
        this.modelSetting = null;
        return;
      }
      try {
        const result = await controlPlaneApi.modelSetting(this.token || null, orgId);
        this.modelSetting = result.model;
      } catch {
        this.modelSetting = null;
      }
    },

    async refreshModelCatalog() {
      try {
        const result = await controlPlaneApi.modelCatalog(this.token || null);
        this.modelCatalog = result.models;
      } catch {
        this.modelCatalog = [];
      }
    },

    async saveModelSetting(model: string) {
      const orgId = this.selectedOrganizationId || this.actor?.organization_id;
      const result = await controlPlaneApi.setModelSetting(this.token || null, model, orgId);
      this.modelSetting = result.model;
      if (this.actor) {
        this.actor = {
          ...this.actor,
          model_name: result.model,
          model_provider: result.catalog_entry?.provider ?? this.actor.model_provider,
          model_base_url: result.catalog_entry?.base_url ?? this.actor.model_base_url,
          model_requires_api_key:
            result.catalog_entry?.requires_api_key ?? this.actor.model_requires_api_key
        };
      }
    },

    async saveModelCatalogEntry(entry: ModelCatalogEntry) {
      const saved = await controlPlaneApi.upsertModelCatalogEntry(this.token || null, entry);
      await this.refreshModelCatalog();
      return saved;
    },

    async refreshProductionGovernance() {
      const orgId = this.selectedOrganizationId || this.actor?.organization_id || null;
      const tasks: Promise<unknown>[] = [];
      if (orgId) {
        tasks.push(
          controlPlaneApi
            .networkPolicy(this.token || null, orgId)
            .then((result) => {
              this.networkPolicy = result.policy;
            })
            .catch(() => {
              this.networkPolicy = null;
            })
        );
      }
      if (this.canManageMembers || this.canViewAudit) {
        tasks.push(
          controlPlaneApi
            .integrations(this.token || null, orgId)
            .then((result) => {
              this.integrations = result.integrations;
            })
            .catch(() => {
              this.integrations = [];
            })
        );
        tasks.push(
          controlPlaneApi
            .approvals(this.token || null, orgId)
            .then((result) => {
              this.approvals = result.approvals;
            })
            .catch(() => {
              this.approvals = [];
            })
        );
      }
      if (orgId) {
        tasks.push(
          controlPlaneApi
            .projectProfiles(this.token || null, orgId)
            .then((result) => {
              this.projectProfiles = result.project_profiles;
            })
            .catch(() => {
              this.projectProfiles = [];
            })
        );
      }
      if (this.canManagePlatform) {
        tasks.push(
          controlPlaneApi
            .productionReadiness(this.token || null)
            .then((result) => {
              this.productionReadiness = result;
            })
            .catch(() => {
              this.productionReadiness = null;
            })
        );
      }
      await Promise.all(tasks);
    },

    async saveNetworkPolicy(policy: NetworkPolicy) {
      const orgId = this.selectedOrganizationId || this.actor?.organization_id || null;
      const result = await controlPlaneApi.updateNetworkPolicy(this.token || null, orgId, policy);
      this.networkPolicy = result.policy;
      if (this.actor) {
        this.actor = { ...this.actor, network_policy: result.policy };
      }
      await this.refreshProductionGovernance();
    },

    async saveIntegration(integration: Omit<IntegrationRecord, 'created_at' | 'updated_at'>) {
      await controlPlaneApi.upsertIntegration(this.token || null, integration);
      await this.refreshProductionGovernance();
    },

    async saveProjectProfile(profile: Omit<ProjectProfile, 'created_at' | 'updated_at'>) {
      await controlPlaneApi.upsertProjectProfile(this.token || null, profile);
      await this.refreshProductionGovernance();
    },

    async createApproval(request: Pick<ApprovalRequest, 'request_type' | 'target' | 'reason'>) {
      await controlPlaneApi.createApproval(this.token || null, {
        ...request,
        organization_id: this.selectedOrganizationId || this.actor?.organization_id || null
      });
      await this.refreshProductionGovernance();
    },

    async decideApproval(approvalId: string, status: 'approved' | 'rejected') {
      await controlPlaneApi.decideApproval(this.token || null, approvalId, status);
      await this.refreshProductionGovernance();
      await this.refreshAudit();
    },

    async refreshSessions() {
      if (!this.actor) {
        this.agentSessions = [];
        return;
      }
      const orgId = this.canManagePlatform ? this.selectedOrganizationId : this.actor?.organization_id;
      const result = await controlPlaneApi.sessions(this.token || null, orgId, null, 100);
      this.agentSessions = result.sessions;
    },

    async refreshAudit() {
      if (!this.canViewAudit) {
        this.auditEvents = [];
        this.auditVerification = null;
        return;
      }
      const orgId = this.canManagePlatform ? this.selectedOrganizationId : this.actor?.organization_id;
      const [events, verification] = await Promise.all([
        controlPlaneApi.auditEvents(this.token || null, orgId, 200),
        this.canManagePlatform ? controlPlaneApi.auditVerify(this.token || null) : Promise.resolve(null)
      ]);
      this.auditEvents = events.events;
      this.auditVerification = verification;
    },

    async createOrganization(name: string, domains: string[] = []) {
      const organization = await controlPlaneApi.createOrganization(this.token || null, name, domains);
      this.organizations = [...this.organizations, organization];
      this.setOrganization(organization.id);
      await this.refreshMembers();
    },

    async saveMember(payload: MemberPayload) {
      const orgId = this.selectedOrganizationId;
      await controlPlaneApi.upsertMember(this.token || null, orgId, payload);
      await this.refreshMembers();
    },

    async removeMember(userId: string) {
      await controlPlaneApi.removeMember(this.token || null, this.selectedOrganizationId, userId);
      await this.refreshMembers();
    },

    async rotateMemberToken(userId: string, token: string) {
      await controlPlaneApi.rotateToken(this.token || null, this.selectedOrganizationId, userId, { token });
    },

    async saveRolePolicy(role: RoleName, allowedTools: string[] | null) {
      const result = await controlPlaneApi.updateRolePolicy(this.token || null, role, allowedTools);
      this.rolePolicies = {
        ...this.rolePolicies,
        [result.role]: result.allowed_tools
      };
    },

    async saveOrgRolePolicy(role: RoleName, allowedTools: string[] | null) {
      const orgId = this.selectedOrganizationId || this.actor?.organization_id;
      if (!orgId) {
        throw new Error('Select an organization first');
      }
      const result = await controlPlaneApi.updateOrgRolePolicy(this.token || null, orgId, role, allowedTools);
      this.orgRolePolicies = {
        ...this.orgRolePolicies,
        [result.role]: result.allowed_tools
      };
      await this.refreshRoleDashboard();
    },

    async requestInternEscalation(reason: string, requestedTool?: string | null) {
      await controlPlaneApi.requestInternEscalation(this.token || null, {
        organization_id: this.selectedOrganizationId || this.actor?.organization_id || null,
        reason,
        requested_tool: requestedTool || null
      });
      await this.refreshRoleDashboard();
    },

    async exportAuditPackage() {
      const orgId = this.canManagePlatform ? this.selectedOrganizationId : this.actor?.organization_id;
      this.auditExport = await controlPlaneApi.auditExport(this.token || null, orgId, 500);
      return this.auditExport;
    },

    async saveUsagePolicy(role: RoleName, policy: UsagePolicy) {
      const result = await controlPlaneApi.updateUsagePolicy(this.token || null, role, policy);
      this.usagePolicies = {
        ...this.usagePolicies,
        [result.role]: result.policy
      };
    },

    async revokeSession(sessionId: string) {
      await controlPlaneApi.revokeSession(this.token || null, sessionId);
      await this.refreshSessions();
    }
  }
});

if (import.meta.hot) {
  import.meta.hot.accept(acceptHMRUpdate(useControlPlaneStore, import.meta.hot));
}
