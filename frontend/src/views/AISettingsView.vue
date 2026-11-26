<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { deleteAIKey, getAISettings, saveAISettings, testAISettings, type AISettings } from '@/api/ai'
import AIPrivacySettings from '@/components/AIPrivacySettings.vue'

const settings = ref<AISettings | null>(null)
const provider = ref<'openai_compatible' | 'anthropic'>('openai_compatible')
const baseUrl = ref('https://api.openai.com/v1')
const model = ref('gpt-4.1-mini')
const apiKey = ref('')
const autoApplyRules = ref(false)
const allowExternalProcessing = ref(false)
const threshold = ref(0.90)
const busy = ref(false)
const testing = ref(false)
const error = ref('')
const notice = ref('')
const testMessage = ref('')

const providerDefaults = computed(() => provider.value === 'anthropic'
  ? { url: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-5' }
  : { url: 'https://api.openai.com/v1', model: 'gpt-4.1-mini' })

function changeProvider(value: 'openai_compatible' | 'anthropic') {
  provider.value = value
  if (!baseUrl.value || baseUrl.value === 'https://api.openai.com/v1' || baseUrl.value === 'https://api.anthropic.com/v1') baseUrl.value = providerDefaults.value.url
  if (!model.value || model.value === 'gpt-4.1-mini' || model.value === 'claude-sonnet-4-5') model.value = providerDefaults.value.model
}

function onProviderChange(event: Event) {
  changeProvider((event.target as HTMLSelectElement).value as 'openai_compatible' | 'anthropic')
}

async function load() {
  busy.value = true
  error.value = ''
  try {
    const value = await getAISettings()
    settings.value = value
    provider.value = value.provider
    baseUrl.value = value.base_url
    model.value = value.model
    autoApplyRules.value = value.auto_apply_rules
    allowExternalProcessing.value = value.allow_external_processing
    threshold.value = value.confidence_threshold
  } catch (err) {
    error.value = err instanceof Error ? err.message : '无法读取 AI 设置'
  } finally {
    busy.value = false
  }
}

async function save() {
  if (busy.value || testing.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  testMessage.value = ''
  try {
    settings.value = await saveAISettings({
      provider: provider.value, base_url: baseUrl.value.trim(), model: model.value.trim(),
      api_key: apiKey.value.trim() || undefined,
      auto_apply_rules: autoApplyRules.value,
      allow_external_processing: allowExternalProcessing.value,
      confidence_threshold: threshold.value,
    })
    apiKey.value = ''
    notice.value = '设置已保存。API Key 只在本机加密保存，页面不会再次显示明文。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    busy.value = false
  }
}

async function testConnection() {
  if (busy.value || testing.value || !settings.value?.configured) return
  testing.value = true
  error.value = ''
  notice.value = ''
  testMessage.value = ''
  try {
    const result = await testAISettings()
    testMessage.value = `${result.message} · ${result.model}`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '连接测试失败'
  } finally {
    testing.value = false
  }
}

async function clearKey() {
  if (!settings.value?.configured || busy.value || testing.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  testMessage.value = ''
  try {
    settings.value = await deleteAIKey()
    allowExternalProcessing.value = false
    apiKey.value = ''
    notice.value = 'API Key 已从本地配置中清除。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '清除失败'
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="workspace-page">
    <header class="workspace-heading">
      <div class="workspace-title">
        <p class="workspace-kicker">AI 工具 / 设置</p>
        <h1>AI 模型设置</h1>
        <p class="workspace-lede">配置模型连接，决定哪些数据可以发送到模型，以及哪些已授权的规则可以自动执行。</p>
      </div>
      <RouterLink class="workspace-button" to="/import">← 返回账单导入</RouterLink>
    </header>

    <div class="settings-layout">
      <form class="settings-form" @submit.prevent="save">
        <section class="workspace-panel settings-card" aria-labelledby="connection-title">
          <div class="section-title">
            <h2 id="connection-title">模型连接</h2>
            <span class="workspace-badge" :class="settings?.configured ? 'workspace-badge--ready' : 'workspace-badge--warn'">
              {{ settings?.configured ? '密钥已保存' : settings ? '尚未配置' : busy ? '读取中…' : '未能读取配置' }}
            </span>
          </div>
          <p class="field-help">保存后可以测试连接。历史账单学习还需要下方的数据处理授权。</p>

          <label class="field-label" for="provider">模型服务商协议</label>
          <select id="provider" class="workspace-field" :value="provider" @change="onProviderChange">
            <option value="openai_compatible">OpenAI 兼容接口（OpenAI、DeepSeek 等）</option>
            <option value="anthropic">Anthropic Claude</option>
          </select>
          <div class="two-columns">
            <div>
              <label class="field-label" for="base-url">API Base URL</label>
              <input id="base-url" v-model="baseUrl" class="workspace-field" type="url" placeholder="https://api.openai.com/v1" autocomplete="url" />
            </div>
            <div>
              <label class="field-label" for="model">模型名称</label>
              <input id="model" v-model="model" class="workspace-field" type="text" placeholder="gpt-4.1-mini" autocomplete="off" />
            </div>
          </div>
          <label class="field-label" for="api-key">API Key</label>
          <input id="api-key" v-model="apiKey" class="workspace-field" type="password" autocomplete="new-password" :placeholder="settings?.configured ? '已保存密钥；留空保持不变' : '粘贴你的 API Key'" />
          <p class="field-help">密钥由本机后端加密保存，页面不会再次显示明文。</p>
        </section>

        <section class="workspace-panel settings-card" aria-labelledby="permission-title">
          <h2 id="permission-title">数据与规则授权</h2>
          <div class="policy-panel">
            <label class="switch-row">
              <input v-model="allowExternalProcessing" type="checkbox" />
              <span><b>允许将必要交易字段发送到模型接口</b><small>分类分析会发送商户名称、商品描述、金额、方向、平台分类和支付方式等必要字段，不会发送交易订单号、银行卡号或完整原始文件。使用云端模型时，数据由对应服务商处理。</small></span>
            </label>
            <p class="field-help">不授权也可以正常导入账单；历史账单学习需要此授权。</p>
          </div>

          <div class="policy-panel">
            <label class="switch-row">
              <input v-model="autoApplyRules" type="checkbox" />
              <span><b>允许已授权规则自动落账</b><small>只执行唯一匹配、分类仍存在且无冲突的已启用规则。仅凭模型高置信度不会自动落账。</small></span>
            </label>
            <div class="threshold-row">
              <label for="threshold">置信度阈值</label>
              <output for="threshold">{{ Math.round(threshold * 100) }}%</output>
            </div>
            <input id="threshold" v-model.number="threshold" type="range" min="0.7" max="0.99" step="0.01" />
            <p class="field-help">阈值用于筛选高置信度建议，不代表分类的实际准确率。</p>
          </div>

          <div class="actions">
            <button class="workspace-button workspace-button--primary" type="submit" :disabled="busy || testing">{{ busy ? '处理中…' : '保存设置' }}</button>
            <button class="workspace-button" type="button" :disabled="busy || testing || !settings?.configured" @click="testConnection">{{ testing ? '测试中…' : '测试已保存的连接' }}</button>
            <button v-if="settings?.configured" class="workspace-button workspace-button--danger" type="button" :disabled="busy || testing" @click="clearKey">清除 API Key</button>
          </div>
          <p v-if="testMessage" class="workspace-note feedback" role="status">{{ testMessage }}</p>
          <p v-if="notice" class="workspace-note feedback" role="status">{{ notice }}</p>
          <p v-if="error" class="workspace-note workspace-note--error feedback" role="alert">{{ error }}</p>
        </section>
      </form>
      <AIPrivacySettings />

      <aside class="workspace-panel settings-card info-card">
        <p class="workspace-kicker">使用前看</p>
        <h2>安全与自动化说明</h2>
        <ul>
          <li>前端只读取密钥是否已配置，不会取得已保存的密钥。</li>
          <li>连接本地模型时，可将 Base URL 指向本机服务。</li>
          <li>AI 建议的新分类须经你批准，才会创建到分类树。</li>
          <li>退款和内部转账等特殊流水不会按普通消费分类。</li>
          <li>当前版本面向本机单用户，不要把后端端口开放到公网或不可信局域网。</li>
        </ul>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.settings-layout { display: grid; grid-template-columns: minmax(0, 2.3fr) minmax(0, 1fr); align-items: start; gap: 16px; }
.settings-form { display: grid; min-width: 0; gap: 16px; }
.settings-card { padding: clamp(16px, 2.5vw, 26px); }
h2 { font-size: 18px; }
.section-title, .threshold-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.field-label { display: block; margin: 14px 0 6px; font-weight: 650; }
.two-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.field-help, .switch-row small, .info-card li { color: var(--workspace-muted); line-height: 1.6; }
.field-help { margin-top: 6px; font-size: 12px; }
.policy-panel { margin-top: 12px; padding: 13px; border-radius: 10px; background: var(--workspace-soft); }
.switch-row { display: flex; align-items: flex-start; gap: 9px; cursor: pointer; }
.switch-row input { flex: none; margin-top: 3px; }
.switch-row small { display: block; margin-top: 4px; font-size: 12px; }
.threshold-row { margin: 12px 0 4px; }
.threshold-row output { color: var(--workspace-accent); font-weight: 750; font-variant-numeric: tabular-nums; }
input[type="range"] { width: 100%; }
input[type="range"], input[type="checkbox"] { accent-color: var(--workspace-accent); }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.feedback { margin-top: 10px; }
.info-card { grid-column: 2; grid-row: 1; background: var(--workspace-tint); overflow-wrap: anywhere; }
.info-card ul { margin: 12px 0 0; padding-left: 18px; }
.info-card li + li { margin-top: 8px; }

@media (max-width: 820px) {
  .settings-layout { grid-template-columns: minmax(0, 1fr); }
  .info-card { grid-area: auto; }
}
@media (max-width: 560px) {
  .two-columns { grid-template-columns: minmax(0, 1fr); gap: 0; }
}
</style>
