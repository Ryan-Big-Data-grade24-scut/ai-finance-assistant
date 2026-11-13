<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { deleteAIKey, getAISettings, saveAISettings, testAISettings, type AISettings } from '@/api/ai'

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
  busy.value = true
  error.value = ''
  notice.value = ''
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
  testing.value = true
  error.value = ''
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
  if (!settings.value?.configured) return
  busy.value = true
  error.value = ''
  notice.value = ''
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
  <div class="ai-settings-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">AI FINANCE WEB</p>
        <h1>AI 模型设置</h1>
        <p class="muted">配置你自己的模型服务。API Key 由后端加密保存，不会发送到浏览器以外的第三方脚本。</p>
      </div>
      <RouterLink class="back-link" to="/import">返回账单导入</RouterLink>
    </div>

    <section class="settings-card">
      <div class="section-title">
        <div><h2>模型连接</h2><p>先连接一个模型，导入账单时就可以自动分析分类。</p></div>
        <span class="status" :class="settings?.configured ? 'ready' : 'not-ready'">{{ settings?.configured ? '已配置' : '尚未配置' }}</span>
      </div>

      <label class="field-label" for="provider">模型服务商协议</label>
      <select id="provider" :value="provider" @change="onProviderChange">
        <option value="openai_compatible">OpenAI 兼容接口（OpenAI、DeepSeek 等）</option>
        <option value="anthropic">Anthropic Claude</option>
      </select>

      <div class="two-columns">
        <div>
          <label class="field-label" for="base-url">API Base URL</label>
          <input id="base-url" v-model="baseUrl" type="url" placeholder="https://api.openai.com/v1" autocomplete="url" />
        </div>
        <div>
          <label class="field-label" for="model">模型名称</label>
          <input id="model" v-model="model" type="text" placeholder="gpt-4.1-mini" autocomplete="off" />
        </div>
      </div>

      <label class="field-label" for="api-key">API Key</label>
      <input id="api-key" v-model="apiKey" type="password" autocomplete="new-password" :placeholder="settings?.configured ? '已保存密钥；留空表示保持不变' : '粘贴你的 API Key'" />
      <p class="field-help">密钥只提交到本机后端，使用 Fernet 加密后存入 SQLite。加密主密钥保存在项目目录之外。不要把项目服务暴露到公网。</p>

      <div class="policy-panel consent-panel">
        <h3>数据处理授权</h3>
        <label class="switch-row consent-row">
          <input v-model="allowExternalProcessing" type="checkbox" />
          <span><b>允许将必要交易字段发送到所配置的模型接口</b><small>整批分类会发送商户名称、商品描述、金额、交易方向、平台分类和支付方式等必要字段。不会发送交易订单号、银行卡号或完整原始文件。若配置云端模型，数据将由对应模型服务商处理。使用本地模型时，可将 Base URL 指向本机服务。</small></span>
        </label>
        <p class="field-help">未勾选时，AI 智能导入不会启动，普通账单导入仍然可用。</p>
      </div>

      <div class="policy-panel">
        <h3>自动分类授权</h3>
        <label class="switch-row">
          <input v-model="autoApplyRules" type="checkbox" />
          <span><b>允许已授权规则自动落账</b><small>仅当交易命中唯一启用的用户规则、分类存在且无冲突时自动执行。单纯模型高置信度不会触发自动落账。</small></span>
        </label>
        <div class="threshold-row">
          <label for="threshold">置信度阈值</label>
          <strong>{{ Math.round(threshold * 100) }}%</strong>
        </div>
        <input id="threshold" v-model.number="threshold" type="range" min="0.7" max="0.99" step="0.01" />
        <p class="field-help">此阈值是附加门槛，不是模型置信度的统计校准保证。自动落账仍必须命中用户明确授权的规则。</p>
      </div>

      <div class="actions">
        <button class="primary" type="button" :disabled="busy" @click="save">{{ busy ? '保存中…' : '保存设置' }}</button>
        <button class="secondary" type="button" :disabled="testing || !settings?.configured" @click="testConnection">{{ testing ? '测试中…' : '测试连接' }}</button>
        <button v-if="settings?.configured" class="danger-ghost" type="button" :disabled="busy" @click="clearKey">清除 API Key</button>
      </div>
      <p v-if="testMessage" class="success-message">{{ testMessage }}</p>
      <p v-if="notice" class="success-message">{{ notice }}</p>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    </section>

    <section class="settings-card info-card">
      <h2>安全与自动化说明</h2>
      <ul>
        <li>API Key 不会返回给前端，设置接口只返回是否已配置。</li>
        <li>只有明确勾选数据处理授权后，AI 才会收到必要的交易字段进行分类分析。</li>
        <li>默认关闭自动落账。开启后，也只会自动执行用户已经明确创建并启用的规则。</li>
        <li>AI 建议的新分类必须先由用户批准，才会创建到 Ledger 分类树。</li>
        <li>退款流水、全额退款、内部转账和退款冲突交易不会被当作普通消费分类。</li>
        <li>当前版本面向本机单用户使用。增加登录和多用户隔离前，不要将后端端口开放到公网或不可信局域网。</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.ai-settings-page { max-width: 900px; margin: 0 auto; color: var(--color-text); }
.page-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; margin: 10px 0 22px; }
.eyebrow { color:#547da8; font-size:11px; letter-spacing:.14em; font-weight:700; margin-bottom:8px; }
h1 { font-size:28px; margin-bottom:8px; }
h2 { font-size:18px; margin-bottom:5px; }
h3 { font-size:15px; margin-bottom:12px; }
.muted,.section-title p,.field-help { color:var(--color-muted); line-height:1.6; }
.back-link { color:#376fae; text-decoration:none; white-space:nowrap; }
.settings-card { background:#fff; border:1px solid var(--color-border); border-radius:14px; padding:22px; margin-bottom:16px; box-shadow:0 8px 28px rgba(28,45,70,.04); }
.section-title { display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:22px; }
.status { padding:5px 10px; border-radius:999px; font-size:12px; white-space:nowrap; }
.status.ready { color:#13784a; background:#e8f7ef; }.status.not-ready { color:#946200; background:#fff5d9; }
.field-label { display:block; font-weight:650; margin:15px 0 7px; }
input:not([type=checkbox]):not([type=range]), select { width:100%; min-height:42px; padding:9px 11px; border:1px solid var(--color-border); border-radius:8px; background:#fff; color:var(--color-text); }
.two-columns { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
.field-help { font-size:12px; margin-top:6px; }
.policy-panel { margin-top:22px; padding:16px; background:#f7f9fc; border-radius:10px; }
.switch-row { display:flex; gap:10px; align-items:flex-start; cursor:pointer; }
.switch-row input { margin-top:4px; }.switch-row b,.switch-row small { display:block; }.switch-row small { color:var(--color-muted); line-height:1.5; margin-top:4px; }
.threshold-row { display:flex; justify-content:space-between; margin:16px 0 6px; }.threshold-row strong { color:#376fae; }
input[type=range] { width:100%; }
.actions { display:flex; flex-wrap:wrap; gap:9px; margin-top:20px; }
button { border:0; border-radius:8px; padding:10px 15px; cursor:pointer; font-weight:650; }
button:disabled { opacity:.55; cursor:not-allowed; }.primary { background:#376fae; color:#fff; }.secondary { background:#eef3f8; color:#35516f; }.danger-ghost { background:#fff0f0; color:#b13b3b; }
.success-message { color:#16804c; margin-top:13px; }.error-message { color:#c33; margin-top:13px; }.info-card li { margin:8px 0 8px 20px; color:#606b78; line-height:1.55; }
@media(max-width:650px){.page-heading{display:block}.back-link{display:inline-block;margin-top:12px}.two-columns{grid-template-columns:1fr}.settings-card{padding:16px}}
</style>
