<template>
  <q-layout view="hHh Lpr lFf" class="console-shell">
    <q-header bordered class="console-header">
      <q-toolbar class="toolbar">
        <q-btn v-if="store.actor" flat dense round icon="menu" aria-label="Navigation" @click="drawerOpen = !drawerOpen" />
        <q-toolbar-title class="title-block">
          <span>Nyansa</span>
          <small>Enterprise Control Plane</small>
        </q-toolbar-title>

        <q-select
          v-if="store.organizations.length > 0"
          v-model="selectedOrganization"
          dense
          outlined
          emit-value
          map-options
          class="org-select"
          :options="organizationOptions"
          aria-label="Organization"
        />

        <q-btn
          flat
          round
          :icon="$q.dark.isActive ? 'light_mode' : 'dark_mode'"
          :aria-label="$q.dark.isActive ? 'Switch to light theme' : 'Switch to dark theme'"
          @click="toggleTheme"
        />
        <q-btn flat round icon="refresh" aria-label="Refresh" :loading="store.loading" @click="refresh" />
        <q-btn v-if="store.actor" flat round icon="logout" aria-label="Sign out" @click="store.signOut()" />
      </q-toolbar>
    </q-header>

    <q-drawer v-if="store.actor" v-model="drawerOpen" show-if-above bordered :width="248" class="console-drawer">
      <div class="identity">
        <q-avatar size="40px" color="primary" text-color="white" icon="admin_panel_settings" />
        <div>
          <strong>{{ store.actor?.user_id || 'Unauthenticated' }}</strong>
          <span>{{ roleLabel(store.actor?.role) }}</span>
        </div>
      </div>

      <q-list padding>
        <q-item
          v-for="item in visibleNavigation"
          :key="item.key"
          clickable
          :active="activeView === item.key"
          active-class="active-nav"
          @click="activeView = item.key"
        >
          <q-item-section avatar>
            <q-icon :name="item.icon" />
          </q-item-section>
          <q-item-section>{{ item.label }}</q-item-section>
        </q-item>
      </q-list>

      <div class="drawer-footer">
        <q-badge :color="store.health?.bootstrapped ? 'positive' : 'warning'" outline>
          {{ store.health?.bootstrapped ? 'Bootstrapped' : 'Needs bootstrap' }}
        </q-badge>
        <q-badge :color="auditValid ? 'positive' : 'negative'" outline>
          Audit {{ auditValid ? 'valid' : 'unverified' }}
        </q-badge>
      </div>
    </q-drawer>

    <q-page-container>
      <q-page class="console-page">
        <q-banner v-if="store.error" dense class="alert-band" rounded>
          <template #avatar>
            <q-icon name="error_outline" color="negative" />
          </template>
          {{ store.error }}
        </q-banner>

        <section v-if="!store.health?.bootstrapped" class="auth-panel">
          <div class="auth-copy">
            <q-icon name="admin_panel_settings" size="44px" color="primary" />
            <h1>Create your admin account</h1>
            <p>This is a one-time setup. It creates your first organization and the
              admin who runs it — you'll sign in with these next.</p>
          </div>
          <q-form class="auth-form" @submit.prevent="bootstrap">
            <q-input v-model="bootstrapForm.organizationName" outlined dense label="Organization name"
              hint="Your company or team name" />
            <q-input v-model="bootstrapForm.rootUserId" outlined dense label="Admin username"
              hint="What you'll sign in with, e.g. admin" />
            <q-input v-model="bootstrapForm.rootDisplayName" outlined dense label="Display name" />
            <q-input v-model="bootstrapForm.rootPassword" outlined dense label="Admin password" type="password"
              hint="You'll use this to sign in to this dashboard" />
            <q-input v-model="bootstrapForm.rootToken" outlined dense label="Recovery / CLI token (optional)" type="password"
              hint="Leave blank — one is generated for you" />
            <q-input v-model="bootstrapForm.bootstrapSecret" outlined dense label="Setup secret" type="password"
              hint="From your .env file: NYANSA_BOOTSTRAP_SECRET (created by the installer)" />
            <q-btn type="submit" color="primary" icon="rocket_launch" label="Create account & organization" :loading="store.loading" />
          </q-form>
        </section>

        <section v-else-if="!store.actor" class="auth-panel">
          <div class="auth-copy">
            <q-icon name="key" size="44px" color="primary" />
            <h1>Sign in</h1>
            <p>Use the admin username and password you just created.</p>
          </div>
          <q-form class="auth-form" @submit.prevent="authenticate">
            <q-input v-model="loginForm.userId" outlined dense label="Username" autofocus />
            <q-input v-model="loginForm.password" outlined dense label="Password" type="password" />
            <q-btn type="submit" color="primary" icon="login" label="Sign in" :loading="store.loading" />
            <q-btn
              v-if="store.setupStatus?.microsoft_sign_in_enabled"
              outline
              color="primary"
              icon="business"
              label="Sign in with Microsoft"
              @click="signInWithMicrosoft"
            />
          </q-form>
        </section>

        <template v-else>
          <section v-if="activeView === 'overview'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Operations overview</h1>
                <p>{{ activeOrgName }} control and compliance posture.</p>
              </div>
              <q-chip square color="primary" text-color="white" icon="verified_user">
                {{ roleLabel(store.actor?.role) }}
              </q-chip>
            </div>

            <div class="metric-grid">
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Readiness</span>
                  <strong>{{ productionReady ? 'Ready' : 'Attention' }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Audit chain</span>
                  <strong :class="{ negative: !auditValid }">{{ auditValid ? 'Valid' : 'Review' }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Organizations</span>
                  <strong>{{ store.organizations.length }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Setup gaps</span>
                  <strong :class="{ negative: setupGapCount > 0 }">{{ setupGapCount }}</strong>
                </q-card-section>
              </q-card>
            </div>

            <q-banner v-if="setupGapCount > 0" class="setup-alert" rounded>
              <template #avatar>
                <q-icon name="priority_high" color="warning" />
              </template>
              {{ setupGapCount }} production setup item{{ setupGapCount === 1 ? '' : 's' }} need attention.
              <template #action>
                <q-btn flat color="primary" label="Open setup" @click="activeView = 'setup'" />
              </template>
            </q-banner>

            <q-banner v-if="store.canManageMembers && store.members.length <= 1" class="setup-alert" rounded>
              <template #avatar>
                <q-icon name="group_add" color="primary" />
              </template>
              You're set up. Next: add your teammates and choose what each role can do.
              <template #action>
                <q-btn flat color="primary" label="Add your team" @click="activeView = 'members'" />
              </template>
            </q-banner>

            <div class="content-grid">
              <section class="panel">
                <div class="panel-title">
                  <h2>Signed in as</h2>
                  <q-icon name="badge" />
                </div>
                <q-list separator>
                  <q-item>
                    <q-item-section>User</q-item-section>
                    <q-item-section side>{{ store.actor?.user_id }}</q-item-section>
                  </q-item>
                  <q-item>
                    <q-item-section>Role</q-item-section>
                    <q-item-section side>{{ roleLabel(store.actor?.role) }}</q-item-section>
                  </q-item>
                  <q-item>
                    <q-item-section>Organization</q-item-section>
                    <q-item-section side>{{ activeOrgName }}</q-item-section>
                  </q-item>
                </q-list>
              </section>

              <section class="panel">
                <div class="panel-title">
                  <h2>What you can do</h2>
                  <q-icon name="tune" />
                </div>
                <div class="q-pa-sm">
                  <div class="text-caption text-grey q-mb-xs">Tools your role can use</div>
                  <div v-if="store.actor?.allowed_tools === null" class="q-mb-sm">
                    All tools (unrestricted)
                  </div>
                  <div v-else-if="store.currentAllowedTools.length === 0" class="q-mb-sm text-grey">
                    No tools enabled for your role
                  </div>
                  <div v-else class="row q-gutter-xs q-mb-sm">
                    <q-chip
                      v-for="tool in store.currentAllowedTools"
                      :key="tool"
                      dense
                      outline
                      color="primary"
                      :label="tool"
                    />
                  </div>

                  <q-separator class="q-my-sm" />

                  <div class="text-caption text-grey q-mb-xs">Your usage limits</div>
                  <div v-if="!store.actor?.usage_policy" class="text-grey">Not enforced</div>
                  <div v-else-if="store.actor.usage_policy.enabled === false" class="text-grey">
                    Model use is disabled for your role.
                  </div>
                  <q-list v-else dense>
                    <q-item>
                      <q-item-section>Requests / minute</q-item-section>
                      <q-item-section side>{{ capLimit(store.actor.usage_policy.requests_per_minute) }}</q-item-section>
                    </q-item>
                    <q-item>
                      <q-item-section>Requests / day</q-item-section>
                      <q-item-section side>{{ capLimit(store.actor.usage_policy.requests_per_day) }}</q-item-section>
                    </q-item>
                    <q-item>
                      <q-item-section>Max session length</q-item-section>
                      <q-item-section side>{{ capLimit(store.actor.usage_policy.max_session_minutes, 'min') }}</q-item-section>
                    </q-item>
                    <q-item>
                      <q-item-section>Max context</q-item-section>
                      <q-item-section side>{{ capLimit(store.actor.usage_policy.max_context_tokens, 'tokens') }}</q-item-section>
                    </q-item>
                  </q-list>
                </div>
              </section>

              <section class="panel">
                <div class="panel-title">
                  <h2>Production setup</h2>
                  <q-icon name="fact_check" />
                </div>
                <q-list separator>
                  <q-item v-for="check in store.setupStatus?.checks ?? []" :key="check.key">
                    <q-item-section avatar>
                      <q-icon :name="check.ok ? 'check_circle' : 'error_outline'" :color="check.ok ? 'positive' : check.severity === 'critical' ? 'negative' : 'warning'" />
                    </q-item-section>
                    <q-item-section>{{ check.label }}</q-item-section>
                    <q-item-section side>
                      <q-badge outline :color="check.ok ? 'positive' : check.severity === 'critical' ? 'negative' : 'warning'">
                        {{ check.ok ? 'Done' : check.severity }}
                      </q-badge>
                    </q-item-section>
                  </q-item>
                </q-list>
              </section>
            </div>
          </section>

          <section v-if="activeView === 'workbench'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>{{ roleLabel(store.roleDashboard?.role ?? store.actor?.role) }} workbench</h1>
                <p>{{ roleWorkbenchCopy }}</p>
              </div>
              <q-chip square color="primary" text-color="white" icon="workspace_premium">
                {{ store.roleDashboard?.active_sessions ?? 0 }} active
              </q-chip>
            </div>

            <div class="metric-grid">
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Visible sessions</span>
                  <strong>{{ store.roleDashboard?.visible_sessions ?? 0 }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Audit events</span>
                  <strong>{{ store.roleDashboard?.audit_events_visible ?? 0 }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Escalations</span>
                  <strong>{{ store.roleDashboard?.pending_escalations ?? 0 }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Audit chain</span>
                  <strong :class="{ negative: !store.roleDashboard?.audit_chain_valid }">
                    {{ store.roleDashboard?.audit_chain_valid ? 'Valid' : 'Review' }}
                  </strong>
                </q-card-section>
              </q-card>
            </div>

            <div class="content-grid">
              <section class="panel">
                <div class="panel-title">
                  <h2>Capabilities</h2>
                  <q-icon name="verified_user" />
                </div>
                <q-list separator>
                  <q-item v-for="capability in store.roleDashboard?.capabilities ?? []" :key="capability">
                    <q-item-section avatar><q-icon name="check_circle" color="positive" /></q-item-section>
                    <q-item-section>{{ capability }}</q-item-section>
                  </q-item>
                </q-list>
              </section>

              <section class="panel">
                <div class="panel-title">
                  <h2>Next actions</h2>
                  <q-icon name="play_circle" />
                </div>
                <q-list separator>
                  <q-item v-for="action in store.roleDashboard?.next_actions ?? []" :key="action">
                    <q-item-section>{{ action }}</q-item-section>
                  </q-item>
                </q-list>
              </section>
            </div>

            <section v-if="store.actor?.role === 'developer'" class="panel">
              <div class="panel-title">
                <h2>Developer CLI</h2>
                <q-icon name="terminal" />
              </div>
              <div class="button-row">
                <q-btn color="primary" icon="key" label="Issue CLI token" @click="issueCliToken" />
                <q-btn v-if="store.issuedCliToken" outline color="primary" icon="content_copy" label="Copy CLI env" @click="copyCliEnv" />
              </div>
              <q-input v-if="store.issuedCliToken" :model-value="cliEnvSnippet" readonly autogrow type="textarea" class="snippet-box" />
            </section>

            <section v-if="store.actor?.role === 'intern'" class="panel">
              <div class="panel-title">
                <h2>Escalation request</h2>
                <q-icon name="approval" />
              </div>
              <div class="setup-form">
                <q-input v-model="internEscalation.requestedTool" outlined dense label="Requested tool or capability" />
                <q-input v-model="internEscalation.reason" outlined dense autogrow type="textarea" label="Reason" />
                <q-btn color="primary" icon="send" label="Submit request" @click="submitInternEscalation" />
              </div>
            </section>

            <section v-if="store.canManageMembers" class="panel">
              <div class="panel-title">
                <h2>Org tool policy</h2>
                <q-icon name="admin_panel_settings" />
              </div>
              <div class="setup-form">
                <q-select v-model="orgPolicyRole" outlined dense emit-value map-options label="Role" :options="restrictedRoleOptions" @update:model-value="syncOrgPolicyTools" />
                <q-toggle v-model="orgPolicyUnrestricted" label="Unrestricted tools for this org role" color="primary" />
                <div class="tool-grid">
                  <q-checkbox
                    v-for="tool in toolCatalog"
                    :key="tool"
                    v-model="orgPolicyTools"
                    :disable="orgPolicyUnrestricted"
                    :label="tool"
                    :val="tool"
                  />
                </div>
                <q-btn color="primary" icon="save" label="Save org policy" @click="saveOrgPolicy" />
              </div>
            </section>

            <section v-if="store.canViewAudit" class="panel">
              <div class="panel-title">
                <h2>Audit evidence</h2>
                <q-icon name="ios_share" />
              </div>
              <div class="button-row">
                <q-btn color="primary" icon="download" label="Export evidence package" @click="exportAuditPackage" />
                <q-btn v-if="store.auditExport" outline color="primary" icon="content_copy" label="Copy export JSON" @click="copyAuditExport" />
              </div>
              <q-banner v-if="store.auditExport" class="result-ok" rounded>
                {{ store.auditExport.events.length }} events and {{ store.auditExport.attestations.length }} attestations exported.
              </q-banner>
            </section>
          </section>

          <section v-if="activeView === 'setup'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Production setup</h1>
                <p>Model provider, secrets, CLI connection, and deployment readiness.</p>
              </div>
              <q-chip square :color="productionReady ? 'positive' : 'warning'" text-color="white" icon="health_and_safety">
                {{ productionReady ? 'Production ready' : 'Setup required' }}
              </q-chip>
            </div>

            <div class="setup-grid">
              <section class="panel">
                <div class="panel-title">
                  <h2>Checklist</h2>
                  <q-icon name="checklist" />
                </div>
                <q-list separator>
                  <q-item v-for="check in store.setupStatus?.checks ?? []" :key="check.key">
                    <q-item-section avatar>
                      <q-icon :name="check.ok ? 'task_alt' : 'radio_button_unchecked'" :color="check.ok ? 'positive' : check.severity === 'critical' ? 'negative' : 'warning'" />
                    </q-item-section>
                    <q-item-section>{{ check.label }}</q-item-section>
                    <q-item-section side>{{ check.ok ? 'OK' : 'Open' }}</q-item-section>
                  </q-item>
                </q-list>
              </section>

              <section class="panel">
                <div class="panel-title">
                  <h2>Model provider</h2>
                  <q-icon name="hub" />
                </div>
                <div class="setup-form">
                  <q-select
                    v-model="setupForm.providerPreset"
                    outlined
                    dense
                    emit-value
                    map-options
                    label="Provider"
                    :options="providerOptions"
                    @update:model-value="applyProviderPreset"
                  />
                  <q-input v-model="setupForm.baseUrl" outlined dense label="Base URL" />
                  <q-input v-model="setupForm.model" outlined dense label="Model" />
                  <q-input v-model="setupForm.apiKeyEnv" outlined dense label="API key environment variable" />
                  <q-input
                    v-model="setupForm.apiKey"
                    outlined
                    dense
                    type="password"
                    label="API key for connectivity check"
                  />
                  <q-banner class="provider-note" rounded>
                    <template #avatar>
                      <q-icon :name="selectedProvider.icon" color="primary" />
                    </template>
                    {{ selectedProvider.note }}
                  </q-banner>
                  <div class="button-row">
                    <q-btn color="primary" icon="network_check" label="Check endpoint" @click="checkModelEndpoint" />
                    <q-btn outline color="primary" icon="content_copy" label="Copy config" @click="copyConfig" />
                  </div>
                  <q-banner
                    v-if="store.modelCheckResult"
                    :class="store.modelCheckResult.ok ? 'result-ok' : 'result-bad'"
                    rounded
                  >
                    <template #avatar>
                      <q-icon :name="store.modelCheckResult.ok ? 'check_circle' : 'error_outline'" :color="store.modelCheckResult.ok ? 'positive' : 'negative'" />
                    </template>
                    {{ store.modelCheckResult.message }}
                    <span v-if="store.modelCheckResult.latency_ms !== null">
                      · {{ store.modelCheckResult.latency_ms }} ms
                    </span>
                  </q-banner>
                </div>
              </section>
            </div>

            <div class="setup-grid">
              <section class="panel">
                <div class="panel-title">
                  <h2>Environment</h2>
                  <q-icon name="terminal" />
                </div>
                <q-input :model-value="envSnippet" readonly autogrow type="textarea" class="snippet-box" />
              </section>

              <section class="panel">
                <div class="panel-title">
                  <h2>Agent config</h2>
                  <q-icon name="settings" />
                </div>
                <q-input :model-value="tomlSnippet" readonly autogrow type="textarea" class="snippet-box" />
              </section>
            </div>

            <section class="panel">
              <div class="panel-title">
                <h2>Connect CLI</h2>
                <q-icon name="vpn_key" />
              </div>
              <div class="setup-form">
                <div class="button-row">
                  <q-btn color="primary" icon="key" label="Issue my CLI token" @click="issueCliToken" />
                  <q-btn
                    v-if="store.issuedCliToken"
                    outline
                    color="primary"
                    icon="content_copy"
                    label="Copy CLI env"
                    @click="copyCliEnv"
                  />
                </div>
                <q-input
                  v-if="store.issuedCliToken"
                  :model-value="cliEnvSnippet"
                  readonly
                  autogrow
                  type="textarea"
                  class="snippet-box"
                />
              </div>
            </section>

            <section v-if="setupForm.providerPreset === 'ollama'" class="panel">
              <div class="panel-title">
                <h2>Ollama guide</h2>
                <q-icon name="dns" />
              </div>
              <div class="guide-steps">
                <code>ollama serve</code>
                <code>ollama pull {{ setupForm.model }}</code>
                <code>curl http://localhost:11434/v1/models</code>
              </div>
            </section>
          </section>

          <section v-if="activeView === 'model'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Model</h1>
                <p>
                  Choose which approved model this organization's agents use. The selected
                  provider and endpoint sync to the CLI the next time agents start.
                </p>
              </div>
              <q-btn
                v-if="store.canManageMembers"
                color="primary"
                icon="save"
                label="Save model"
                @click="saveModel"
              />
            </div>

            <q-card flat bordered>
              <q-card-section class="column q-gutter-md">
                <div class="text-caption text-grey">
                  Current model:
                  <strong>{{ store.modelSetting ?? 'using each environment’s local default' }}</strong>
                </div>
                <q-select
                  v-model="modelInput"
                  outlined
                  dense
                  emit-value
                  map-options
                  label="Approved model"
                  :options="modelOptions"
                  :readonly="!store.canManageMembers"
                  @keyup.enter="store.canManageMembers ? saveModel() : undefined"
                />
                <div v-if="store.modelSetting" class="row q-col-gutter-sm">
                  <q-chip v-if="store.actor?.model_provider" dense outline color="primary" icon="hub">
                    {{ store.actor.model_provider }}
                  </q-chip>
                  <q-chip v-if="store.actor?.model_base_url" dense outline color="secondary" icon="link">
                    {{ store.actor.model_base_url }}
                  </q-chip>
                  <q-chip dense outline :color="store.actor?.model_requires_api_key ? 'warning' : 'positive'" icon="key">
                    {{ store.actor?.model_requires_api_key ? 'API key required' : 'No API key required' }}
                  </q-chip>
                </div>
                <div v-else class="text-caption text-grey">
                  Only organization admins can change the model.
                </div>
                <q-banner dense class="bg-grey-2 text-grey-8">
                  Agents pick up a new model the next time they start.
                </q-banner>
              </q-card-section>
            </q-card>

            <q-card v-if="store.canManagePlatform" flat bordered>
              <q-card-section class="panel-title">
                <div>
                  <h2>Approved Catalog</h2>
                  <p>Root and platform owners control which model endpoints organizations can select.</p>
                </div>
                <q-btn color="primary" icon="add" label="Save catalog entry" @click="saveModelCatalogEntry" />
              </q-card-section>
              <q-card-section class="grid-2">
                <q-input v-model="modelCatalogForm.id" outlined dense label="Model ID" />
                <q-input v-model="modelCatalogForm.label" outlined dense label="Label" />
                <q-select
                  v-model="modelCatalogForm.provider"
                  outlined
                  dense
                  emit-value
                  map-options
                  label="Provider"
                  :options="modelProviderOptions"
                />
                <q-input v-model="modelCatalogForm.base_url" outlined dense label="Base URL" />
                <q-toggle v-model="modelCatalogForm.requires_api_key" label="Requires API key" color="primary" />
                <q-toggle v-model="modelCatalogForm.enabled" label="Enabled" color="primary" />
                <q-toggle v-model="modelCatalogForm.free" label="Free model" color="primary" />
                <q-input v-model="modelCatalogForm.notes" outlined dense label="Notes" />
              </q-card-section>
              <q-card-section>
                <q-list bordered separator>
                  <q-item v-for="entry in store.modelCatalog" :key="entry.id">
                    <q-item-section>
                      <q-item-label>{{ entry.label }}</q-item-label>
                      <q-item-label caption>{{ entry.id }} · {{ entry.provider }} · {{ entry.base_url ?? 'local command' }}</q-item-label>
                    </q-item-section>
                    <q-item-section side>
                      <q-chip dense :color="entry.enabled ? 'positive' : 'grey'" text-color="white">
                        {{ entry.enabled ? 'enabled' : 'disabled' }}
                      </q-chip>
                    </q-item-section>
                  </q-item>
                </q-list>
              </q-card-section>
            </q-card>
          </section>

          <section v-if="activeView === 'governance'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Governance</h1>
                <p>Production controls for agent egress, approvals, integrations, and launch readiness.</p>
              </div>
              <q-chip
                v-if="store.productionReadiness"
                square
                :color="store.productionReadiness.ready ? 'positive' : 'warning'"
                text-color="white"
                icon="verified"
              >
                {{ store.productionReadiness.score }}/10
              </q-chip>
            </div>

            <q-card v-if="store.canManagePlatform && store.productionReadiness" flat bordered>
              <q-card-section class="panel-title">
                <div>
                  <h2>Production Readiness</h2>
                  <p>{{ store.productionReadiness.ready ? 'All tracked production checks are passing.' : 'Tracked launch blockers that still need owner review.' }}</p>
                </div>
              </q-card-section>
              <q-card-section>
                <div class="readiness-pills">
                  <q-chip
                    v-for="(ok, check) in store.productionReadiness.checks"
                    :key="check"
                    class="readiness-pill"
                    dense
                    outline
                    :color="ok ? 'positive' : 'warning'"
                    :icon="ok ? 'check_circle' : 'error_outline'"
                  >
                    {{ String(check).replaceAll('_', ' ') }}
                  </q-chip>
                </div>
              </q-card-section>
            </q-card>

            <q-card flat bordered>
              <q-card-section class="panel-title">
                <div>
                  <h2>Network Policy</h2>
                  <p>Org-level egress posture synced to agent startup.</p>
                </div>
                <q-btn v-if="store.canManageMembers" color="primary" icon="save" label="Save network policy" @click="saveNetworkPolicy" />
              </q-card-section>
              <q-card-section class="grid-2">
                <q-toggle v-model="networkForm.allow_network" label="Allow shell network access" color="primary" :disable="!store.canManageMembers" />
                <q-toggle v-model="networkForm.require_proxy" label="Require proxy" color="primary" :disable="!store.canManageMembers" />
                <q-input v-model="networkAllowedDomains" outlined dense label="Allowed domains" hint="Comma-separated" :readonly="!store.canManageMembers" />
                <q-input v-model="networkBlockedDomains" outlined dense label="Blocked domains" hint="Comma-separated" :readonly="!store.canManageMembers" />
                <q-input v-model="networkForm.proxy_url" outlined dense label="Proxy URL" :readonly="!store.canManageMembers" />
              </q-card-section>
            </q-card>

            <q-card v-if="store.canManageMembers || store.canViewAudit" flat bordered>
              <q-card-section class="panel-title">
                <div>
                  <h2>Integrations</h2>
                  <p>Registry for source control, ticketing, chat, identity, SIEM, and MCP connectors.</p>
                </div>
                <q-btn v-if="store.canManageMembers" color="primary" icon="add_link" label="Save integration" @click="saveIntegration" />
              </q-card-section>
              <q-card-section v-if="store.canManageMembers" class="grid-2">
                <q-input v-model="integrationForm.name" outlined dense label="Name" />
                <q-select v-model="integrationForm.provider" outlined dense emit-value map-options label="Provider" :options="integrationProviderOptions" />
                <q-toggle v-model="integrationForm.enabled" label="Enabled" color="primary" />
                <q-input v-model="integrationConfigText" outlined dense autogrow type="textarea" label="Config JSON" />
              </q-card-section>
              <q-card-section>
                <q-list bordered separator>
                  <q-item v-for="integration in store.integrations" :key="integration.id">
                    <q-item-section>
                      <q-item-label>{{ integration.name }}</q-item-label>
                      <q-item-label caption>{{ integration.provider }} · {{ integration.organization_id ?? 'platform' }}</q-item-label>
                    </q-item-section>
                    <q-item-section side>
                      <q-chip dense :color="integration.enabled ? 'positive' : 'grey'" text-color="white">
                        {{ integration.enabled ? 'enabled' : 'disabled' }}
                      </q-chip>
                    </q-item-section>
                  </q-item>
                </q-list>
              </q-card-section>
            </q-card>

            <q-card flat bordered>
              <q-card-section class="panel-title">
                <div>
                  <h2>Approval Queue</h2>
                  <p>Requests for elevated tools, network access, models, workspaces, and integrations.</p>
                </div>
                <q-btn color="primary" icon="send" label="Request approval" @click="createApproval" />
              </q-card-section>
              <q-card-section class="grid-2">
                <q-select v-model="approvalForm.request_type" outlined dense emit-value map-options label="Type" :options="approvalTypeOptions" />
                <q-input v-model="approvalForm.target" outlined dense label="Target" />
                <q-input v-model="approvalForm.reason" outlined dense autogrow type="textarea" label="Reason" />
              </q-card-section>
              <q-card-section>
                <q-list bordered separator>
                  <q-item v-for="approval in store.approvals" :key="approval.id">
                    <q-item-section>
                      <q-item-label>{{ approval.request_type }} · {{ approval.target }}</q-item-label>
                      <q-item-label caption>{{ approval.requester_user_id }} · {{ approval.status }} · {{ approval.reason }}</q-item-label>
                    </q-item-section>
                    <q-item-section v-if="store.canManageMembers && approval.status === 'pending'" side>
                      <div class="row q-gutter-xs">
                        <q-btn dense flat round color="positive" icon="check" @click="decideApproval(approval.id, 'approved')" />
                        <q-btn dense flat round color="negative" icon="close" @click="decideApproval(approval.id, 'rejected')" />
                      </div>
                    </q-item-section>
                  </q-item>
                </q-list>
              </q-card-section>
            </q-card>
          </section>

          <section v-if="activeView === 'organizations'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Organizations</h1>
                <p>Tenant boundaries for teams using the coding agent.</p>
              </div>
              <q-btn
                v-if="store.canManagePlatform"
                color="primary"
                icon="domain_add"
                label="Create organization"
                @click="organizationDialog = true"
              />
            </div>

            <q-table
              flat
              bordered
              class="data-table"
              :dense="$q.screen.lt.sm"
              :rows="store.organizations"
              :columns="organizationColumns"
              row-key="id"
              :pagination="{ rowsPerPage: 10 }"
            >
              <template #body-cell-name="props">
                <q-td :props="props">
                  <q-btn flat dense no-caps color="primary" :label="props.row.name" @click="store.setOrganization(props.row.id)" />
                </q-td>
              </template>
            </q-table>
          </section>

          <section v-if="activeView === 'members'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Members</h1>
                <p>Role assignment and token rotation for {{ activeOrgName }}.</p>
              </div>
              <q-btn
                v-if="store.canManageMembers"
                color="primary"
                icon="person_add"
                label="Add member"
                :disable="!store.selectedOrganizationId"
                @click="openMemberDialog()"
              />
            </div>

            <q-table
              flat
              bordered
              class="data-table"
              :dense="$q.screen.lt.sm"
              :rows="store.members"
              :columns="memberColumns"
              row-key="user_id"
              :pagination="{ rowsPerPage: 12 }"
            >
              <template #body-cell-role="props">
                <q-td :props="props">
                  <q-badge outline color="primary">{{ roleLabel(props.row.role) }}</q-badge>
                </q-td>
              </template>
              <template #body-cell-actions="props">
                <q-td :props="props" class="row-actions">
                  <q-btn flat round dense icon="edit" aria-label="Edit member" @click="openMemberDialog(props.row)" />
                  <q-btn flat round dense icon="vpn_key" aria-label="Rotate token" @click="openRotateDialog(props.row.user_id)" />
                  <q-btn flat round dense icon="delete" color="negative" aria-label="Remove member" @click="removeMember(props.row.user_id)" />
                </q-td>
              </template>
              <template #no-data>
                <div class="full-width column flex-center q-pa-lg text-grey">
                  <q-icon name="group_add" size="32px" />
                  <div class="q-mt-sm">No teammates yet — click “Add member” to invite your first one.</div>
                </div>
              </template>
            </q-table>
          </section>

          <section v-if="activeView === 'sessions'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Agent sessions</h1>
                <p>Live CLI activity synced from developers, interns, and admins.</p>
              </div>
              <q-btn color="primary" icon="refresh" label="Refresh" @click="store.refreshSessions()" />
            </div>

            <q-table
              flat
              bordered
              class="data-table"
              :dense="$q.screen.lt.sm"
              :rows="store.agentSessions"
              :columns="sessionColumns"
              row-key="id"
              :pagination="{ rowsPerPage: 12 }"
            >
              <template #body-cell-status="props">
                <q-td :props="props">
                  <q-badge :color="sessionStatusColor(props.row.status)" outline>{{ props.row.status }}</q-badge>
                </q-td>
              </template>
              <template #body-cell-cwd="props">
                <q-td :props="props">
                  <code>{{ compactPath(props.row.cwd) }}</code>
                </q-td>
              </template>
              <template #body-cell-actions="props">
                <q-td :props="props" class="row-actions">
                  <q-btn
                    v-if="store.canManageMembers && props.row.status === 'active'"
                    flat
                    round
                    dense
                    color="negative"
                    icon="block"
                    aria-label="Revoke session"
                    @click="revokeSession(props.row.id)"
                  />
                </q-td>
              </template>
              <template #no-data>
                <div class="full-width column flex-center q-pa-lg text-grey">
                  <q-icon name="computer" size="32px" />
                  <div class="q-mt-sm">No sessions yet — they appear here when someone runs the CLI (<code>uv run nyansa</code>).</div>
                </div>
              </template>
            </q-table>
          </section>

          <section v-if="activeView === 'usage'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Usage limits</h1>
                <p>Caps for shared model fallback keys and role-based execution budgets.</p>
              </div>
              <q-btn color="primary" icon="save" label="Save selected role" @click="saveSelectedUsagePolicy" />
            </div>

            <div class="metric-grid">
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Events today</span>
                  <strong>{{ store.usageSummary?.event_count ?? 0 }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Developers today</span>
                  <strong>{{ store.usageSummary?.last_day.developer ?? 0 }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Interns today</span>
                  <strong>{{ store.usageSummary?.last_day.intern ?? 0 }}</strong>
                </q-card-section>
              </q-card>
              <q-card flat bordered class="metric-card">
                <q-card-section>
                  <span>Last hour</span>
                  <strong>{{ usageLastHourTotal }}</strong>
                </q-card-section>
              </q-card>
            </div>

            <section class="policy-panel">
              <q-tabs v-model="selectedUsageRole" dense align="left" class="policy-tabs">
                <q-tab v-for="role in roles" :key="role" :name="role" :label="roleLabel(role)" />
              </q-tabs>
              <q-separator />
              <div class="usage-grid">
                <q-toggle v-model="usageForm.enabled" label="Allow model usage for this role" color="primary" />
                <q-input v-model.number="usageForm.requests_per_minute" outlined dense type="number" label="Requests per minute" />
                <q-input v-model.number="usageForm.requests_per_hour" outlined dense type="number" label="Requests per hour" />
                <q-input v-model.number="usageForm.requests_per_day" outlined dense type="number" label="Requests per user per day" />
                <q-input v-model.number="usageForm.org_requests_per_day" outlined dense type="number" label="Requests per org per day" />
                <q-input v-model.number="usageForm.max_turns_per_session" outlined dense type="number" label="Max turns per session" />
                <q-input v-model.number="usageForm.max_session_minutes" outlined dense type="number" label="Max session minutes" />
                <q-input v-model.number="usageForm.max_context_tokens" outlined dense type="number" label="Max context tokens" />
              </div>
            </section>
          </section>

          <section v-if="activeView === 'policies'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Role policies</h1>
                <p>Tool access enforced by the backend before execution.</p>
              </div>
              <q-btn color="primary" icon="save" label="Save selected role" @click="saveSelectedPolicy" />
            </div>

            <section class="policy-panel">
              <q-tabs v-model="selectedPolicyRole" dense align="left" class="policy-tabs">
                <q-tab v-for="role in roles" :key="role" :name="role" :label="roleLabel(role)" />
              </q-tabs>
              <q-separator />
              <div class="policy-body">
                <q-toggle
                  v-model="selectedPolicyUsesDefaults"
                  label="Use unrestricted policy for this role"
                  color="primary"
                />
                <div class="tool-grid">
                  <q-checkbox
                    v-for="tool in toolCatalog"
                    :key="tool"
                    v-model="selectedPolicyTools"
                    :val="tool"
                    :label="tool"
                    :disable="selectedPolicyUsesDefaults"
                    dense
                  />
                </div>
              </div>
            </section>
          </section>

          <section v-if="activeView === 'audit'" class="view-stack">
            <div class="view-heading">
              <div>
                <h1>Audit</h1>
                <p>Redacted event history and integrity verification.</p>
              </div>
              <q-chip square :color="auditValid ? 'positive' : 'negative'" text-color="white" icon="fact_check">
                {{ auditValid ? 'Chain valid' : 'Needs review' }}
              </q-chip>
            </div>

            <q-table
              flat
              bordered
              class="data-table"
              :dense="$q.screen.lt.sm"
              :rows="store.auditEvents"
              :columns="auditColumns"
              row-key="id"
              :pagination="{ rowsPerPage: 15 }"
            >
              <template #body-cell-event_type="props">
                <q-td :props="props">
                  <q-badge :color="eventColor(props.row.event_type)" outline>{{ props.row.event_type }}</q-badge>
                </q-td>
              </template>
              <template #body-cell-actor="props">
                <q-td :props="props">{{ actorSummary(props.row.actor) }}</q-td>
              </template>
              <template #body-cell-details="props">
                <q-td :props="props">
                  <code>{{ compactJson(props.row.details) }}</code>
                </q-td>
              </template>
              <template #no-data>
                <div class="full-width column flex-center q-pa-lg text-grey">
                  <q-icon name="fact_check" size="32px" />
                  <div class="q-mt-sm">No activity yet — every agent action will be recorded here.</div>
                </div>
              </template>
            </q-table>
          </section>
        </template>
      </q-page>
    </q-page-container>

    <q-dialog v-model="organizationDialog">
      <q-card class="dialog-card">
        <q-card-section>
          <h2>Create organization</h2>
        </q-card-section>
        <q-card-section class="dialog-form">
          <q-input v-model="organizationName" outlined dense label="Organization name" autofocus />
          <q-input v-model="organizationDomains" outlined dense label="Microsoft domains" hint="Comma-separated, e.g. acme.com, engineering.acme.com" />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat label="Cancel" v-close-popup />
          <q-btn color="primary" icon="domain_add" label="Create" @click="createOrganization" />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="memberDialog">
      <q-card class="dialog-card">
        <q-card-section>
          <h2>{{ memberForm.editing ? 'Update member' : 'Add member' }}</h2>
        </q-card-section>
        <q-card-section class="dialog-form">
          <q-input v-model="memberForm.userId" outlined dense label="User ID" :disable="memberForm.editing" />
          <q-input v-model="memberForm.displayName" outlined dense label="Display name" />
          <q-select v-model="memberForm.role" outlined dense emit-value map-options label="Role" :options="roleOptions" />
          <q-input
            v-model="memberForm.password"
            outlined
            dense
            type="password"
            label="Password"
          />
          <q-input
            v-model="memberForm.token"
            outlined
            dense
            type="password"
            label="Recovery / CLI token (optional)"
          />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat label="Cancel" v-close-popup />
          <q-btn color="primary" icon="save" label="Save" @click="saveMember" />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="rotateDialog">
      <q-card class="dialog-card">
        <q-card-section>
          <h2>Rotate token</h2>
        </q-card-section>
        <q-card-section>
          <q-input v-model="rotateTokenValue" outlined dense type="password" label="New token" autofocus />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat label="Cancel" v-close-popup />
          <q-btn color="primary" icon="vpn_key" label="Rotate" @click="rotateToken" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-layout>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useQuasar, type QTableColumn } from 'quasar';
import {
  roles,
  toolCatalog,
  useControlPlaneStore
} from 'src/stores/control-plane';
import { controlPlaneApi, type ApprovalRequest, type IntegrationRecord, type Member, type ModelCatalogEntry, type ModelProvider, type NetworkPolicy, type Organization, type RoleName, type UsagePolicy } from 'src/api/controlPlane';

type ViewKey = 'overview' | 'workbench' | 'setup' | 'model' | 'governance' | 'organizations' | 'members' | 'sessions' | 'usage' | 'policies' | 'audit';

const $q = useQuasar();
const store = useControlPlaneStore();

const drawerOpen = ref(false);
const activeView = ref<ViewKey>('overview');
const organizationDialog = ref(false);
const organizationName = ref('');
const organizationDomains = ref('');
const memberDialog = ref(false);
const rotateDialog = ref(false);
const rotateUserId = ref('');
const rotateTokenValue = ref('');
const modelInput = ref('');
const orgPolicyRole = ref<RoleName>('intern');
const orgPolicyTools = ref<string[]>([]);
const orgPolicyUnrestricted = ref(false);
const internEscalation = reactive({
  requestedTool: '',
  reason: ''
});
const networkForm = reactive<NetworkPolicy>({
  allow_network: false,
  allowed_domains: [],
  blocked_domains: [],
  require_proxy: false,
  proxy_url: null
});
const networkAllowedDomains = ref('');
const networkBlockedDomains = ref('');
const integrationForm = reactive<Omit<IntegrationRecord, 'created_at' | 'updated_at'>>({
  id: '',
  provider: 'github',
  name: '',
  organization_id: null,
  enabled: false,
  config: {}
});
const integrationConfigText = ref('{}');
const approvalForm = reactive<Pick<ApprovalRequest, 'request_type' | 'target' | 'reason'>>({
  request_type: 'tool',
  target: '',
  reason: ''
});
const modelCatalogForm = reactive<ModelCatalogEntry>({
  id: '',
  label: '',
  provider: 'openai_compatible',
  base_url: 'https://openrouter.ai/api/v1',
  requires_api_key: true,
  enabled: true,
  free: false,
  notes: ''
});

const modelOptions = computed(() =>
  store.modelCatalog
    .filter((entry) => entry.enabled || store.canManagePlatform)
    .map((entry) => ({
      label: `${entry.label} (${entry.id})`,
      value: entry.id
    }))
);

const modelProviderOptions = [
  { label: 'OpenAI-compatible', value: 'openai_compatible' },
  { label: 'Anthropic API', value: 'anthropic' },
  { label: 'Claude Code', value: 'claude_code' }
];

const integrationProviderOptions = [
  'github',
  'gitlab',
  'jira',
  'slack',
  'siem',
  'scim',
  'saml',
  'mcp',
  'custom'
].map((value) => ({ label: value.toUpperCase(), value }));

const approvalTypeOptions = [
  'tool',
  'network',
  'model',
  'workspace',
  'integration',
  'other'
].map((value) => ({ label: value, value }));

watch(
  () => store.modelSetting,
  (value) => {
    modelInput.value = value ?? '';
  },
  { immediate: true }
);
watch(
  () => store.networkPolicy,
  (policy) => {
    networkForm.allow_network = policy?.allow_network ?? false;
    networkForm.allowed_domains = [...(policy?.allowed_domains ?? [])];
    networkForm.blocked_domains = [...(policy?.blocked_domains ?? [])];
    networkForm.require_proxy = policy?.require_proxy ?? false;
    networkForm.proxy_url = policy?.proxy_url ?? null;
    networkAllowedDomains.value = networkForm.allowed_domains.join(', ');
    networkBlockedDomains.value = networkForm.blocked_domains.join(', ');
  },
  { immediate: true }
);
const selectedPolicyRole = ref<RoleName>('developer');
const selectedPolicyTools = ref<string[]>([]);
const selectedPolicyUsesDefaults = ref(false);
const selectedUsageRole = ref<RoleName>('developer');
const usageForm = reactive<UsagePolicy>({
  enabled: true,
  requests_per_minute: null,
  requests_per_hour: null,
  requests_per_day: null,
  org_requests_per_day: null,
  max_turns_per_session: null,
  max_session_minutes: null,
  max_context_tokens: null
});
const setupForm = reactive({
  providerPreset: 'openrouter',
  provider: 'openai_compatible' as ModelProvider,
  baseUrl: 'https://openrouter.ai/api/v1',
  model: 'poolside/laguna-s-2.1:free',
  apiKeyEnv: 'NYANSA_API_KEY',
  apiKey: ''
});

const bootstrapForm = reactive({
  organizationName: 'Default Organization',
  rootUserId: 'root',
  rootDisplayName: 'Root User',
  rootPassword: '',
  rootToken: '',
  bootstrapSecret: ''
});

const loginForm = reactive({
  userId: 'root',
  password: ''
});

const memberForm = reactive({
  editing: false,
  userId: '',
  displayName: '',
  role: 'developer' as RoleName,
  password: '',
  token: ''
});

const navigation = [
  { key: 'overview' as const, label: 'Overview', icon: 'space_dashboard', guard: () => true },
  { key: 'workbench' as const, label: 'Workbench', icon: 'workspace_premium', guard: () => Boolean(store.actor) },
  { key: 'setup' as const, label: 'Setup', icon: 'tune', guard: () => store.canManagePlatform },
  { key: 'model' as const, label: 'Model', icon: 'smart_toy', guard: () => Boolean(store.actor) },
  { key: 'governance' as const, label: 'Governance', icon: 'gpp_maybe', guard: () => Boolean(store.actor) },
  { key: 'organizations' as const, label: 'Organizations', icon: 'domain', guard: () => true },
  { key: 'members' as const, label: 'Members', icon: 'group', guard: () => store.canManageMembers },
  { key: 'sessions' as const, label: 'Sessions', icon: 'computer', guard: () => Boolean(store.actor) },
  { key: 'usage' as const, label: 'Usage', icon: 'speed', guard: () => store.canManagePlatform },
  { key: 'policies' as const, label: 'Policies', icon: 'policy', guard: () => store.canManagePlatform },
  { key: 'audit' as const, label: 'Audit', icon: 'fact_check', guard: () => store.canViewAudit }
];

const fallbackProvider = {
  label: 'Ollama self-hosted',
  value: 'ollama',
  provider: 'openai_compatible' as ModelProvider,
  baseUrl: 'http://localhost:11434/v1',
  model: 'qwen2.5-coder:7b',
  apiKeyEnv: 'NYANSA_API_KEY',
  icon: 'dns',
  note: 'Runs inside your environment through Ollama OpenAI-compatible endpoints. No external model company receives prompts when the endpoint stays private.'
};

const openRouterProvider = {
  label: 'OpenRouter (free default)',
  value: 'openrouter',
  provider: 'openai_compatible' as ModelProvider,
  baseUrl: 'https://openrouter.ai/api/v1',
  model: 'poolside/laguna-s-2.1:free',
  apiKeyEnv: 'NYANSA_API_KEY',
  icon: 'router',
  note: 'The app default. Free coding models work (no per-token charge) but need a free OpenRouter key and are rate-limited; switch to a paid model like openai/gpt-4o-mini for higher limits. Free-model availability rotates — see openrouter.ai/models?max_price=0.'
};

const providerOptions = [
  openRouterProvider,
  fallbackProvider,
  {
    label: 'OpenAI / Codex-style',
    value: 'openai',
    provider: 'openai_compatible' as ModelProvider,
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4o-mini',
    apiKeyEnv: 'NYANSA_API_KEY',
    icon: 'cloud',
    note: 'Uses an OpenAI-compatible API. Prompts are sent to the configured external provider unless routed through your own gateway.'
  },
  {
    label: 'Claude API direct check',
    value: 'claude',
    provider: 'anthropic' as ModelProvider,
    baseUrl: 'https://api.anthropic.com',
    model: 'claude-sonnet-4-20250514',
    apiKeyEnv: 'ANTHROPIC_API_KEY',
    icon: 'psychology',
    note: 'Runs through Anthropic Messages API with this app\'s native tool loop. Requires an Anthropic API key; Claude.ai Pro/Max subscription billing is separate.'
  },
  {
    label: 'Claude Code subscription',
    value: 'claude_code',
    provider: 'claude_code' as ModelProvider,
    baseUrl: 'local',
    model: 'sonnet',
    apiKeyEnv: 'CLAUDE_CODE_OAUTH_TOKEN',
    icon: 'terminal',
    note: 'Uses each developer machine\'s Claude Code login, including supported Pro/Max subscription auth. Install Claude Code locally and run `claude` to authenticate before launching the CLI.'
  },
  {
    label: 'OpenAI-compatible gateway',
    value: 'gateway',
    provider: 'openai_compatible' as ModelProvider,
    baseUrl: 'https://llm-gateway.example.com/v1',
    model: 'company-coding-model',
    apiKeyEnv: 'NYANSA_API_KEY',
    icon: 'router',
    note: 'Use this for LiteLLM, vLLM, internal model gateways, or Claude routed through a company-controlled OpenAI-compatible endpoint.'
  }
];

const visibleNavigation = computed(() => navigation.filter((item) => item.guard()));
const auditValid = computed(() => store.auditVerification?.valid ?? store.ready?.audit.valid ?? true);
const productionReady = computed(() => Boolean(store.setupStatus?.ok && auditValid.value));
const setupGapCount = computed(() => (store.setupStatus?.checks ?? []).filter((check) => !check.ok).length);
const activeOrgName = computed(() => store.activeOrganization?.name ?? store.actor?.organization_id ?? 'No organization');
const usageLastHourTotal = computed(() =>
  Object.values(store.usageSummary?.last_hour ?? {}).reduce((total, count) => total + count, 0)
);
const organizationOptions = computed(() =>
  store.organizations.map((org: Organization) => ({ label: org.name, value: org.id }))
);
const roleOptions = roles.map((role: RoleName) => ({ label: roleLabel(role), value: role }));
const restrictedRoleOptions = (['developer', 'intern', 'auditor'] as RoleName[]).map((role) => ({
  label: roleLabel(role),
  value: role
}));
const selectedProvider = computed(
  () => providerOptions.find((provider) => provider.value === setupForm.providerPreset) ?? fallbackProvider
);
const roleWorkbenchCopy = computed(() => {
  const role = store.roleDashboard?.role ?? store.actor?.role;
  if (role === 'root' || role === 'platform_owner') {
    return 'Platform controls, readiness, policy, and evidence review.';
  }
  if (role === 'org_owner') {
    return 'Team onboarding, organization settings, and scoped policy control.';
  }
  if (role === 'security_admin') {
    return 'Session oversight, escalation triage, token hygiene, and audit review.';
  }
  if (role === 'developer') {
    return 'Self-service CLI setup, usage visibility, and governed coding sessions.';
  }
  if (role === 'intern') {
    return 'Guided safe tooling with a clear path to request more access.';
  }
  if (role === 'auditor') {
    return 'Read-only evidence review, attestations, and exportable audit packages.';
  }
  return 'Role-aware controls for your current organization.';
});
const envSnippet = computed(() => {
  if (setupForm.providerPreset === 'claude_code') {
    return [
      '# Claude Code mode uses your local `claude` login; no Anthropic API key is required.',
      '# Optional for managed environments: export CLAUDE_CODE_OAUTH_TOKEN="<oauth-token>"',
      `export NYANSA_CONTROL_TOKEN="${store.issuedCliToken || '<issue-cli-token-first>'}"`,
      'export NYANSA_BOOTSTRAP_SECRET="<set-long-random-bootstrap-secret>"'
    ].join('\n');
  }

  const keyLine = setupForm.apiKey
    ? `export ${setupForm.apiKeyEnv}="${setupForm.apiKey}"`
    : `export ${setupForm.apiKeyEnv}="<paste-provider-api-key-here>"`;
  return [
    keyLine,
    `export NYANSA_CONTROL_TOKEN="${store.issuedCliToken || '<issue-cli-token-first>'}"`,
    'export NYANSA_BOOTSTRAP_SECRET="<set-long-random-bootstrap-secret>"'
  ].join('\n');
});
const cliEnvSnippet = computed(() =>
  [
    `export NYANSA_CONTROL_TOKEN="${store.issuedCliToken || '<issue-token-first>'}"`,
    'export NYANSA_CONTROL_BASE_URL="http://localhost:8080"'
  ].join('\n')
);
const tomlSnippet = computed(() => {
  const requiresKey = !['ollama', 'claude_code'].includes(setupForm.providerPreset);
  const modelLines = [
    '[model]',
    `provider = "${setupForm.provider}"`,
    `name = "${setupForm.model}"`,
    `requires_api_key = ${requiresKey ? 'true' : 'false'}`,
  ];

  if (setupForm.providerPreset === 'claude_code') {
    modelLines.push(
      'claude_code_command = "claude"',
      'claude_code_permission_mode = "default"'
    );
  } else {
    modelLines.splice(
      3,
      0,
      `base_url = "${setupForm.baseUrl}"`,
      `api_key_env = "${setupForm.apiKeyEnv}"`
    );
  }

  return [
    ...modelLines,
    '',
    '[control_plane]',
    'enabled = true',
    'base_url = "http://localhost:8080"',
    'token_env = "NYANSA_CONTROL_TOKEN"',
    'fail_closed = true',
    'sync_actor = true',
    'send_audit_events = true',
    '',
    '[audit]',
    'enabled = true',
    'log_content = false',
    '',
    '[sensitive_data]',
    'enabled = true',
    'redact_before_model = true',
    'redact_audit_events = true'
  ].join('\n');
});

const selectedOrganization = computed({
  get: () => store.selectedOrganizationId,
  set: async (value: string) => {
    store.setOrganization(value);
    await store.refreshAll();
  }
});

const organizationColumns: QTableColumn[] = [
  { name: 'name', label: 'Name', field: 'name', align: 'left', sortable: true },
  { name: 'id', label: 'Organization ID', field: 'id', align: 'left' },
  { name: 'created_at', label: 'Created', field: 'created_at', align: 'left', format: formatDate, sortable: true }
];

const memberColumns: QTableColumn[] = [
  { name: 'user_id', label: 'User', field: 'user_id', align: 'left', sortable: true },
  { name: 'role', label: 'Role', field: 'role', align: 'left', sortable: true },
  { name: 'created_at', label: 'Joined', field: 'created_at', align: 'left', format: formatDate, sortable: true },
  { name: 'actions', label: '', field: 'actions', align: 'right' }
];

const auditColumns: QTableColumn[] = [
  { name: 'timestamp', label: 'Time', field: 'timestamp', align: 'left', format: formatDate, sortable: true },
  { name: 'event_type', label: 'Event', field: 'event_type', align: 'left', sortable: true },
  { name: 'actor', label: 'Actor', field: 'actor', align: 'left' },
  { name: 'details', label: 'Details', field: 'details', align: 'left' },
  { name: 'event_hash', label: 'Hash', field: 'event_hash', align: 'left', format: shortHash }
];

const sessionColumns: QTableColumn[] = [
  { name: 'status', label: 'Status', field: 'status', align: 'left', sortable: true },
  { name: 'user_id', label: 'User', field: 'user_id', align: 'left', sortable: true },
  { name: 'role', label: 'Role', field: 'role', align: 'left', format: roleLabel, sortable: true },
  { name: 'model', label: 'Model', field: 'model', align: 'left' },
  { name: 'cwd', label: 'Workspace', field: 'cwd', align: 'left' },
  { name: 'turn_count', label: 'Turns', field: 'turn_count', align: 'right', sortable: true },
  { name: 'last_heartbeat_at', label: 'Last seen', field: 'last_heartbeat_at', align: 'left', format: formatDate, sortable: true },
  { name: 'actions', label: '', field: 'actions', align: 'right' }
];

const THEME_KEY = 'nyansa.theme';

function applyStoredTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  // Default to the OS preference; honor an explicit choice once made.
  $q.dark.set(saved === 'dark' ? true : saved === 'light' ? false : 'auto');
}

function toggleTheme() {
  const next = !$q.dark.isActive;
  $q.dark.set(next);
  localStorage.setItem(THEME_KEY, next ? 'dark' : 'light');
}

onMounted(async () => {
  applyStoredTheme();
  await store.refreshAll();
});

watch(
  () => store.rolePolicies[selectedPolicyRole.value],
  () => syncSelectedPolicy(),
  { immediate: true }
);

watch(selectedPolicyRole, () => syncSelectedPolicy());
watch(
  () => store.usagePolicies[selectedUsageRole.value],
  () => syncSelectedUsagePolicy(),
  { immediate: true }
);
watch(selectedUsageRole, () => syncSelectedUsagePolicy());
watch(
  () => store.orgRolePolicies,
  () => syncOrgPolicyTools(),
  { deep: true }
);
watch(orgPolicyRole, () => syncOrgPolicyTools());

async function refresh() {
  await store.refreshAll();
}

async function bootstrap() {
  await store.bootstrap({ ...bootstrapForm });
  $q.notify({ type: 'positive', message: 'Control plane bootstrapped' });
}

async function authenticate() {
  await store.login(loginForm.userId, loginForm.password);
  if (store.actor) {
    $q.notify({ type: 'positive', message: `Signed in as ${store.actor.user_id}` });
  }
}

function signInWithMicrosoft() {
  window.location.href = controlPlaneApi.microsoftStartUrl();
}

async function issueCliToken() {
  await store.issueCliToken();
  $q.notify({ type: 'positive', message: 'CLI token issued' });
}

function syncOrgPolicyTools() {
  const policy = store.orgRolePolicies[orgPolicyRole.value];
  orgPolicyUnrestricted.value = policy === null;
  orgPolicyTools.value = policy ? [...policy] : [];
}

async function saveOrgPolicy() {
  await store.saveOrgRolePolicy(
    orgPolicyRole.value,
    orgPolicyUnrestricted.value ? null : orgPolicyTools.value
  );
  $q.notify({ type: 'positive', message: 'Organization policy saved' });
}

async function submitInternEscalation() {
  await store.requestInternEscalation(
    internEscalation.reason,
    internEscalation.requestedTool
  );
  internEscalation.reason = '';
  internEscalation.requestedTool = '';
  $q.notify({ type: 'positive', message: 'Escalation request submitted' });
}

async function exportAuditPackage() {
  await store.exportAuditPackage();
  $q.notify({ type: 'positive', message: 'Audit package exported' });
}

function applyProviderPreset(value: string) {
  const provider = providerOptions.find((option) => option.value === value);
  if (!provider) {
    return;
  }
  setupForm.provider = provider.provider;
  setupForm.baseUrl = provider.baseUrl;
  setupForm.model = provider.model;
  setupForm.apiKeyEnv = provider.apiKeyEnv;
  setupForm.apiKey = '';
}

async function checkModelEndpoint() {
  const result = await store.checkModel({
    provider: setupForm.provider,
    base_url: setupForm.baseUrl,
    model: setupForm.model,
    api_key: setupForm.apiKey || null
  });
  $q.notify({
    type: result.ok ? 'positive' : 'negative',
    message: result.ok ? 'Model endpoint is reachable' : 'Model endpoint check failed'
  });
}

async function copyConfig() {
  await navigator.clipboard.writeText(`${envSnippet.value}\n\n# .nyansa/config.toml\n${tomlSnippet.value}`);
  $q.notify({ type: 'positive', message: 'Setup config copied' });
}

async function copyCliEnv() {
  await navigator.clipboard.writeText(cliEnvSnippet.value);
  $q.notify({ type: 'positive', message: 'CLI environment copied' });
}

async function copyAuditExport() {
  await navigator.clipboard.writeText(JSON.stringify(store.auditExport, null, 2));
  $q.notify({ type: 'positive', message: 'Audit export copied' });
}

async function createOrganization() {
  if (!organizationName.value.trim()) {
    return;
  }
  const domains = organizationDomains.value
    .split(',')
    .map((domain) => domain.trim().toLowerCase())
    .filter(Boolean);
  await store.createOrganization(organizationName.value.trim(), domains);
  organizationName.value = '';
  organizationDomains.value = '';
  organizationDialog.value = false;
  $q.notify({ type: 'positive', message: 'Organization created' });
}

function openMemberDialog(member?: Member) {
  memberForm.editing = Boolean(member);
  memberForm.userId = member?.user_id ?? '';
  memberForm.displayName = '';
  memberForm.role = member?.role ?? 'developer';
  memberForm.password = '';
  memberForm.token = '';
  memberDialog.value = true;
}

async function saveMember() {
  await store.saveMember({
    user_id: memberForm.userId.trim(),
    display_name: memberForm.displayName.trim(),
    role: memberForm.role,
    password: memberForm.password || null,
    token: memberForm.token || null
  });
  memberDialog.value = false;
  $q.notify({ type: 'positive', message: 'Member saved' });
}

async function removeMember(userId: string) {
  await store.removeMember(userId);
  $q.notify({ type: 'positive', message: 'Member removed' });
}

function openRotateDialog(userId: string) {
  rotateUserId.value = userId;
  rotateTokenValue.value = '';
  rotateDialog.value = true;
}

async function rotateToken() {
  await store.rotateMemberToken(rotateUserId.value, rotateTokenValue.value);
  rotateDialog.value = false;
  $q.notify({ type: 'positive', message: 'Token rotated' });
}

async function revokeSession(sessionId: string) {
  await store.revokeSession(sessionId);
  $q.notify({ type: 'positive', message: 'Session revoked' });
}

async function saveSelectedPolicy() {
  await store.saveRolePolicy(
    selectedPolicyRole.value,
    selectedPolicyUsesDefaults.value ? null : selectedPolicyTools.value
  );
  $q.notify({ type: 'positive', message: 'Policy saved' });
}

async function saveSelectedUsagePolicy() {
  await store.saveUsagePolicy(selectedUsageRole.value, normalizeUsagePolicy(usageForm));
  $q.notify({ type: 'positive', message: 'Usage policy saved' });
}

async function saveModel() {
  const model = modelInput.value.trim();
  if (!model) {
    $q.notify({ type: 'warning', message: 'Enter a model name' });
    return;
  }
  try {
    await store.saveModelSetting(model);
    $q.notify({ type: 'positive', message: `Model set to ${model}` });
  } catch (error) {
    $q.notify({ type: 'negative', message: error instanceof Error ? error.message : 'Could not set model' });
  }
}

async function saveModelCatalogEntry() {
  if (!modelCatalogForm.id.trim() || !modelCatalogForm.label.trim()) {
    $q.notify({ type: 'warning', message: 'Model ID and label are required' });
    return;
  }
  try {
    const saved = await store.saveModelCatalogEntry({
      ...modelCatalogForm,
      id: modelCatalogForm.id.trim(),
      label: modelCatalogForm.label.trim(),
      base_url: modelCatalogForm.base_url?.trim() || null,
      notes: modelCatalogForm.notes.trim()
    });
    modelCatalogForm.id = '';
    modelCatalogForm.label = '';
    modelCatalogForm.notes = '';
    $q.notify({ type: 'positive', message: `Catalog entry saved: ${saved.id}` });
  } catch (error) {
    $q.notify({ type: 'negative', message: error instanceof Error ? error.message : 'Could not save catalog entry' });
  }
}

function splitDomains(value: string) {
  return value
    .split(',')
    .map((item) => item.trim().toLowerCase().replace(/^@/, ''))
    .filter(Boolean);
}

async function saveNetworkPolicy() {
  try {
    await store.saveNetworkPolicy({
      ...networkForm,
      allowed_domains: splitDomains(networkAllowedDomains.value),
      blocked_domains: splitDomains(networkBlockedDomains.value),
      proxy_url: networkForm.proxy_url?.trim() || null
    });
    $q.notify({ type: 'positive', message: 'Network policy saved' });
  } catch (error) {
    $q.notify({ type: 'negative', message: error instanceof Error ? error.message : 'Could not save network policy' });
  }
}

async function saveIntegration() {
  try {
    const config = JSON.parse(integrationConfigText.value || '{}') as Record<string, unknown>;
    await store.saveIntegration({
      ...integrationForm,
      id: integrationForm.id || '',
      name: integrationForm.name.trim(),
      organization_id: store.selectedOrganizationId || store.actor?.organization_id || null,
      config
    });
    integrationForm.id = '';
    integrationForm.name = '';
    integrationForm.enabled = false;
    integrationConfigText.value = '{}';
    $q.notify({ type: 'positive', message: 'Integration saved' });
  } catch (error) {
    $q.notify({ type: 'negative', message: error instanceof Error ? error.message : 'Could not save integration' });
  }
}

async function createApproval() {
  if (!approvalForm.target.trim() || !approvalForm.reason.trim()) {
    $q.notify({ type: 'warning', message: 'Target and reason are required' });
    return;
  }
  try {
    await store.createApproval({
      request_type: approvalForm.request_type,
      target: approvalForm.target.trim(),
      reason: approvalForm.reason.trim()
    });
    approvalForm.target = '';
    approvalForm.reason = '';
    $q.notify({ type: 'positive', message: 'Approval requested' });
  } catch (error) {
    $q.notify({ type: 'negative', message: error instanceof Error ? error.message : 'Could not create approval' });
  }
}

async function decideApproval(approvalId: string, status: 'approved' | 'rejected') {
  try {
    await store.decideApproval(approvalId, status);
    $q.notify({ type: 'positive', message: `Approval ${status}` });
  } catch (error) {
    $q.notify({ type: 'negative', message: error instanceof Error ? error.message : 'Could not update approval' });
  }
}

function syncSelectedPolicy() {
  const policy = store.rolePolicies[selectedPolicyRole.value];
  selectedPolicyUsesDefaults.value = policy === null;
  selectedPolicyTools.value = policy ? [...policy] : [];
}

function syncSelectedUsagePolicy() {
  const policy = store.usagePolicies[selectedUsageRole.value];
  if (!policy) {
    return;
  }
  Object.assign(usageForm, policy);
}

function normalizeUsagePolicy(policy: UsagePolicy): UsagePolicy {
  return {
    enabled: policy.enabled,
    requests_per_minute: positiveNumberOrNull(policy.requests_per_minute),
    requests_per_hour: positiveNumberOrNull(policy.requests_per_hour),
    requests_per_day: positiveNumberOrNull(policy.requests_per_day),
    org_requests_per_day: positiveNumberOrNull(policy.org_requests_per_day),
    max_turns_per_session: positiveNumberOrNull(policy.max_turns_per_session),
    max_session_minutes: positiveNumberOrNull(policy.max_session_minutes),
    max_context_tokens: positiveNumberOrNull(policy.max_context_tokens)
  };
}

function positiveNumberOrNull(value: number | null) {
  if (value === null || Number.isNaN(value) || value <= 0) {
    return null;
  }
  return Math.floor(value);
}

function roleLabel(role?: string | null) {
  if (!role) {
    return 'No role';
  }
  return role
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function capLimit(value: number | null | undefined, unit = ''): string {
  if (value === null || value === undefined) {
    return 'No limit';
  }
  return unit ? `${value.toLocaleString()} ${unit}` : value.toLocaleString();
}

function formatDate(value: string) {
  return value ? new Date(value).toLocaleString() : '';
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 10)}...` : '';
}

function actorSummary(actor: Record<string, unknown>) {
  const userId = actor.user_id ?? actor.userId ?? 'system';
  const role = actor.role ? ` (${actor.role})` : '';
  return `${userId}${role}`;
}

function compactJson(value: Record<string, unknown>) {
  const text = JSON.stringify(value);
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
}

function eventColor(eventType: string) {
  if (eventType.includes('denied') || eventType.includes('error')) {
    return 'negative';
  }
  if (eventType.includes('approval') || eventType.includes('policy')) {
    return 'warning';
  }
  return 'primary';
}

function sessionStatusColor(status: string) {
  if (status === 'active') {
    return 'positive';
  }
  if (status === 'revoked') {
    return 'negative';
  }
  return 'grey';
}

function compactPath(path: string) {
  if (path.length <= 46) {
    return path;
  }
  return `...${path.slice(-43)}`;
}
</script>
