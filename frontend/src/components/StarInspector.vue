<script setup lang="ts">
import type { LedgerNode, LedgerTree } from '@/api/ledger'
import type { Point } from '@/stars/useStarGraph'
const props=defineProps<{
  selected: Point
  nodeSelector: LedgerNode[]
  tree: LedgerTree|null
  targetNodeId: string
  busy: boolean
}>()
const emit=defineEmits<{
  (e:'close'):void
  (e:'move-tx'):void
  (e:'change-op'):void
  (e:'update:targetNodeId', value:string):void
}>()
const fmt=(n:number)=>new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}).format(n)
function onSelect(event:Event){emit('update:targetNodeId',(event.target as HTMLSelectElement).value)}
function getGroupTrail(id:string):string {
  if(!props.tree)return '暂无分类'
  const nodes=new Map(props.tree.nodes.map(n=>[n.id,n]))
  const path:string[]=[]
  let node=nodes.get(id)
  while(node){if(node.kind!=='root')path.unshift(node.name);node=node.parent_id?nodes.get(node.parent_id):undefined}
  return path.join(' / ')||'未分类'
}
</script>
<template>
        <aside v-if="selected" class="inspector">
          <div class="inspector-top"><span>NODE INSPECTOR <small>节点详情</small></span><button title="收起详情" @click="emit('close')">×</button></div>
          <div class="inspector-content">
            <div class="inspect-kind">{{ selected.node.kind === 'tx' ? 'TRANSACTION' : selected.node.kind === 'root' ? 'MASTER LEDGER' : selected.node.side === 'time' ? 'TIME CLUSTER' : 'CATEGORY CLUSTER' }}</div>
            <h3>{{ selected.node.name }}</h3>
            <div v-if="selected.node.tx" class="inspect-transaction">
              <div class="inspect-amount">{{ selected.node.tx.direction === 'expense' ? '−' : '+' }} ¥{{ fmt(selected.node.tx.effective_amount ?? selected.node.tx.amount) }}</div>
              <div v-if="(selected.node.tx.refunded_amount ?? 0) > 0" class="inspect-line"><span>原始金额</span><b>¥{{ fmt(selected.node.tx.amount) }}</b></div>
              <div v-if="(selected.node.tx.refunded_amount ?? 0) > 0" class="inspect-line"><span>已退款累计</span><b>− ¥{{ fmt(selected.node.tx.refunded_amount ?? 0) }}</b></div>
              <div v-if="(selected.node.tx.refunded_amount ?? 0) > 0" class="inspect-line"><span>计入账本</span><b>¥{{ fmt(selected.node.tx.effective_amount ?? selected.node.tx.amount) }}</b></div>
              <div v-if="(selected.node.tx.refund_zeroed || selected.node.tx.internal_transfer_zeroed) && !((selected.node.tx.refunded_amount ?? 0) > 0)" class="inspect-line"><span>原始流水金额</span><b>¥{{ fmt(selected.node.tx.amount) }}</b></div>
              <div v-if="selected.node.tx.refund_conflict" class="refund-warning">退款累计金额与关联明细存在冲突。当前金额按平台累计值计算，暂不允许外部额度分裂，请核对原始账单。</div>
              <div v-else-if="selected.node.tx.refund_zeroed" class="inspect-note">该交易已全额退款或属于退款流水，不会重复计入收入/支出。</div>
              <div v-else-if="selected.node.tx.internal_transfer_zeroed" class="inspect-note">该交易已识别为本人账户内部转账，不计入收入/支出。</div>
              <div class="inspect-line"><span>时间</span><b>{{ selected.node.tx.tx_time }}</b></div>
              <div class="inspect-line"><span>交易对象</span><b>{{ selected.node.tx.counterparty || '—' }}</b></div>
              <div class="inspect-line"><span>商品说明</span><b>{{ selected.node.tx.item || '—' }}</b></div>
              <div class="inspect-line"><span>原始来源</span><b>{{ selected.node.tx.source }}</b></div>
              <div class="inspect-line"><span>目前归属</span><b>{{ getGroupTrail(selected.node.tx.node_id) }}</b></div>
              <div v-if="selected.node.side === 'category'" class="reassign-panel">
                <label for="tx-target">将此交易调整到</label>
                <select id="tx-target" :value="targetNodeId" @change="onSelect($event)"><option v-for="n in nodeSelector" :key="n.id" :value="n.id">{{ n.kind === 'root' ? '未分类（根）' : n.name }}</option></select>
                <button class="action-btn" :disabled="busy || targetNodeId === selected.node.tx.node_id" @click="emit('move-tx')">确认调整归属 ↗</button>
              </div>
              <p v-else class="inspect-note">时间是原始事实，不能拖拽修改。到分类视角移动此笔交易。</p>
            </div>
            <template v-else>
              <div class="inspect-total">¥ {{ fmt(selected.node.e + selected.node.i) }}</div>
              <div class="inspect-stats"><div><span>支出</span><b>¥{{ fmt(selected.node.e) }}</b></div><div><span>收入</span><b>¥{{ fmt(selected.node.i) }}</b></div><div><span>包含记录</span><b>{{ selected.node.count }} 笔</b></div></div>
              <div v-if="selected.node.op && selected.node.nodeId && selected.node.nodeId !== tree?.root_id" class="op-panel">
                <span>聚合方式</span><button :class="{net: selected.node.op === 'NET'}" :disabled="busy" @click="emit('change-op')">{{ selected.node.op }} ↔</button>
              </div>
              <p class="inspect-note">点击一个节点，它会放大并成为画面中心；其他分支暂时隐藏。通过「返回上一级」或顶部路径返回。</p>
            </template>
          </div>
        </aside>
</template>
<style scoped>
.inspector{position:absolute;z-index:4;right:22px;bottom:23px;width:283px;max-height:71%;overflow-y:auto;color:#dfe7fc;border:1px solid #afc2fa35;background:linear-gradient(145deg,#1a2543f2,#121c33f7);backdrop-filter:blur(24px);box-shadow:0 20px 60px #05091b77;border-radius:14px;scrollbar-color:#5e6882 transparent;scrollbar-width:thin;}
.inspector-top{display:flex;align-items:center;justify-content:space-between;padding:15px 17px;border-bottom:1px solid #ffffff13;font-size:10px;font-weight:750;letter-spacing:1.45px;color:#9eaee0;}.inspector-top small{font-size:10px;letter-spacing:.1px;color:#7c8dac;font-weight:400;margin-left:5px;}.inspector-top button{color:#afbcdd;border:0;background:transparent;font-size:21px;line-height:1;}
.inspector-content{padding:19px 17px 20px;}.inspect-kind{font-size:9px;color:#81d3cf;letter-spacing:2px;font-weight:650;margin-bottom:7px;}.inspector h3{font-size:18px;line-height:1.5;color:#f3f6ff;font-weight:680;overflow-wrap:anywhere;}.inspect-total{font-size:24px;font-weight:760;letter-spacing:-.4px;color:#eef3ff;margin:10px 0 18px;}.inspect-stats{display:grid;gap:10px;}.inspect-stats>div{display:flex;justify-content:space-between;gap:10px;padding-bottom:10px;border-bottom:1px solid #ffffff15;font-size:11px;color:#8ea0c4;}.inspect-stats b{color:#dae5ff;font-weight:650;}
.inspect-note{font-size:11px;color:#7f91b6;line-height:1.65;margin-top:16px;}.refund-warning{margin:12px 0;padding:10px 11px;border:1px solid #d6a65c66;border-radius:8px;background:#d6a65c14;color:#f2cc91;font-size:11px;line-height:1.65;}.inspect-amount{font-size:24px;color:#f8c692;font-weight:760;margin:12px 0 18px;}.inspect-line{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #ffffff13;font-size:11px;line-height:1.6;}.inspect-line span{color:#92a2c3;white-space:nowrap;}.inspect-line b{color:#dce4f8;text-align:right;font-weight:550;overflow-wrap:anywhere;}
.reassign-panel,.op-panel{margin-top:18px;display:grid;gap:9px;font-size:11px;color:#aebde1;}.reassign-panel select,.galaxy-modal select,.galaxy-modal input{height:37px;border:1px solid #ffffff30;background:#223250;border-radius:8px;color:#e3edff;max-width:100%;padding:0 9px;outline:0;}.reassign-panel select option{background:#15223a;}.action-btn{padding:10px 12px;border:0;color:#fff;font-weight:650;background:#6777d2;border-radius:8px;}.action-btn:hover{background:#7f8be2;}.op-panel{grid-template-columns:1fr auto;align-items:center;}.op-panel button{padding:6px 12px;border:1px solid #718ac280;background:#263755;color:#b4e4de;border-radius:6px;font-size:11px;}.op-panel button.net{color:#fad38f;border-color:#d0a86b66;}
@media(max-width:1050px){.inspector{width:260px}}
@media(max-width:660px){.inspector{max-height:47%;width:calc(100% - 40px);right:20px;bottom:60px}}
</style>
