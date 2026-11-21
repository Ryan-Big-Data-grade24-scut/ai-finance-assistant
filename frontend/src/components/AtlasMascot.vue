<script setup lang="ts">
/**
 * ATLAS 的公仔。
 *
 * 三套形象（星仪 / 账灵 / 夜枭）共用同一组状态：待命会眨眼，运行中头顶冒点，
 * 面板打开时眼睛弯起来。只是装饰，不参与任何业务状态。
 */
withDefaults(defineProps<{
  variant?: number
  state?: 'idle' | 'thinking' | 'open'
}>(), {
  variant: 0,
  state: 'idle',
})
</script>

<template>
  <svg class="atlas-mascot" :class="[`v${variant % 3}`, `is-${state}`]"
       viewBox="0 0 80 96" role="img" aria-hidden="true">
    <!-- 运行中的思考点：三套形象共用 -->
    <g class="think-dots">
      <circle cx="30" cy="13" r="2.6" />
      <circle cx="40" cy="9" r="2.6" />
      <circle cx="50" cy="13" r="2.6" />
    </g>

    <template v-if="variant % 3 === 0">
      <defs>
        <radialGradient id="atlas-orb" cx="34%" cy="26%" r="85%">
          <stop offset="0%" stop-color="#a9d8ff" />
          <stop offset="52%" stop-color="#4d88c8" />
          <stop offset="100%" stop-color="#1c4d84" />
        </radialGradient>
        <linearGradient id="atlas-gold" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#ffe9b0" />
          <stop offset="100%" stop-color="#d3a13d" />
        </linearGradient>
      </defs>
      <ellipse class="shadow" cx="40" cy="90" rx="17" ry="4.5" />
      <line x1="40" y1="26" x2="40" y2="16" stroke="#7fb3e8" stroke-width="2.6" stroke-linecap="round" />
      <circle class="antenna-star" cx="40" cy="13.5" r="3.2" />
      <ellipse class="ring" cx="40" cy="57" rx="29" ry="10.5" transform="rotate(-18 40 57)"
               fill="none" stroke="url(#atlas-gold)" stroke-width="3" />
      <g class="moon">
        <circle cx="66" cy="48" r="4.4" fill="#ffe08a" />
      </g>
      <circle cx="40" cy="56" r="20" fill="url(#atlas-orb)" />
      <circle cx="40" cy="56" r="20" fill="none" stroke="rgba(255,255,255,.28)" stroke-width="1.4" />
      <g class="eye">
        <circle cx="33.5" cy="54" r="4.6" fill="#f4fbff" />
        <circle class="pupil" cx="34.5" cy="54.4" r="2.2" />
      </g>
      <g class="eye">
        <circle cx="46.5" cy="54" r="4.6" fill="#f4fbff" />
        <circle class="pupil" cx="45.5" cy="54.4" r="2.2" />
      </g>
      <path class="mouth" d="M35.5 63 Q40 66.5 44.5 63" />
    </template>

    <template v-else-if="variant % 3 === 1">
      <defs>
        <radialGradient id="atlas-coin" cx="36%" cy="28%" r="85%">
          <stop offset="0%" stop-color="#ffe9a8" />
          <stop offset="60%" stop-color="#f2c258" />
          <stop offset="100%" stop-color="#c58f27" />
        </radialGradient>
      </defs>
      <ellipse class="shadow" cx="40" cy="89" rx="16" ry="4.2" />
      <ellipse cx="29" cy="82" rx="6" ry="3.4" fill="#c58f27" />
      <ellipse cx="51" cy="82" rx="6" ry="3.4" fill="#c58f27" />
      <circle cx="40" cy="55" r="22" fill="url(#atlas-coin)" />
      <circle cx="40" cy="55" r="18.5" fill="none" stroke="rgba(255,255,255,.5)" stroke-width="1.6"
              stroke-dasharray="3 3.2" />
      <text x="40" y="41" text-anchor="middle" font-size="11" font-weight="800"
            fill="#8a6416" font-family="Georgia, serif">¥</text>
      <g class="eye">
        <circle cx="33.5" cy="54" r="4.2" fill="#5b430f" />
        <circle cx="35" cy="52.6" r="1.4" fill="#fff6de" />
      </g>
      <g class="eye">
        <circle cx="46.5" cy="54" r="4.2" fill="#5b430f" />
        <circle cx="48" cy="52.6" r="1.4" fill="#fff6de" />
      </g>
      <path class="mouth" d="M36 63 Q40 66.5 44 63" />
      <g class="spark">
        <path d="M62 34 l1.6 4 4 1.6 -4 1.6 -1.6 4 -1.6 -4 -4 -1.6 4 -1.6 Z" fill="#fff4cf" />
      </g>
    </template>

    <template v-else>
      <defs>
        <radialGradient id="atlas-owl" cx="36%" cy="26%" r="88%">
          <stop offset="0%" stop-color="#5686bd" />
          <stop offset="62%" stop-color="#2c5488" />
          <stop offset="100%" stop-color="#17365a" />
        </radialGradient>
      </defs>
      <ellipse class="shadow" cx="40" cy="89" rx="18" ry="4.5" />
      <path d="M24 42 L22 27 L35 35 Z" fill="#1b3f68" />
      <path d="M56 42 L58 27 L45 35 Z" fill="#1b3f68" />
      <ellipse cx="40" cy="57" rx="22" ry="24" fill="url(#atlas-owl)" />
      <ellipse cx="20" cy="62" rx="7" ry="13" fill="#244a78" />
      <ellipse cx="60" cy="62" rx="7" ry="13" fill="#244a78" />
      <g class="eye">
        <circle cx="32" cy="51" r="9.2" fill="#f4ead0" />
        <circle class="pupil" cx="32" cy="51.4" r="4" fill="#1a2c40" />
        <circle cx="33.6" cy="49.8" r="1.5" fill="#fff" />
      </g>
      <g class="eye">
        <circle cx="48" cy="51" r="9.2" fill="#f4ead0" />
        <circle class="pupil" cx="48" cy="51.4" r="4" fill="#1a2c40" />
        <circle cx="49.6" cy="49.8" r="1.5" fill="#fff" />
      </g>
      <polygon points="40,56 35.5,63 44.5,63" fill="#f0a43c" />
      <path d="M30 78 q10 6 20 0" fill="none" stroke="rgba(255,255,255,.22)" stroke-width="2"
            stroke-linecap="round" />
    </template>
  </svg>
</template>

<style scoped>
.atlas-mascot { display: block; width: 100%; height: 100%; overflow: visible; }
.shadow { fill: rgba(24, 52, 84, .16); }
.pupil { fill: #12263c; }
.mouth { fill: none; stroke: #eaf6ff; stroke-width: 2; stroke-linecap: round; }
.antenna-star { fill: #ffd76e; filter: drop-shadow(0 0 4px rgba(255, 215, 110, .9)); }
.spark { transform-origin: 63px 38px; animation: atlas-twinkle 2.8s ease-in-out infinite; }

/* 眨眼：只压扁眼白和瞳孔，不带动整张脸 */
.eye { transform-box: fill-box; transform-origin: center; animation: atlas-blink 4.6s infinite; }
.is-open .eye { animation: atlas-happy 1.8s ease-in-out infinite; }
.is-thinking .eye { animation: none; }

/* 星仪：小卫星绕轨道转 */
.moon {
  transform-box: view-box; transform-origin: 40px 57px;
  animation: atlas-orbit 7s linear infinite;
}
.is-thinking .moon { animation-duration: 1.6s; }
.is-thinking .ring { animation: atlas-pulse 1.2s ease-in-out infinite; }

.think-dots { opacity: 0; transition: opacity .2s; }
.think-dots circle { fill: #8fc3f4; }
.is-thinking .think-dots { opacity: 1; }
.think-dots circle:nth-child(1) { animation: atlas-bounce 1s infinite; }
.think-dots circle:nth-child(2) { animation: atlas-bounce 1s .15s infinite; }
.think-dots circle:nth-child(3) { animation: atlas-bounce 1s .3s infinite; }

@keyframes atlas-blink {
  0%, 91%, 100% { transform: scaleY(1); }
  95% { transform: scaleY(.12); }
}
@keyframes atlas-happy {
  0%, 100% { transform: scaleY(1); }
  50% { transform: scaleY(.62); }
}
@keyframes atlas-orbit {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
@keyframes atlas-pulse {
  0%, 100% { opacity: .9; }
  50% { opacity: .45; }
}
@keyframes atlas-twinkle {
  0%, 100% { transform: scale(.7) rotate(0deg); opacity: .55; }
  50% { transform: scale(1.15) rotate(18deg); opacity: 1; }
}
@keyframes atlas-bounce {
  0%, 100% { transform: translateY(0); opacity: .5; }
  50% { transform: translateY(-3px); opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .eye, .moon, .spark, .think-dots circle, .is-thinking .ring { animation: none; }
}
</style>
