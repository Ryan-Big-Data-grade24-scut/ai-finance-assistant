<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { Group, WebGLRenderer } from 'three'

const props = defineProps<{ thinking?: boolean; speaking?: boolean }>()
const emit = defineEmits<{ portrait: [url: string] }>()
const stage = ref<HTMLDivElement | null>(null)
const ready = ref(false)
let dispose = () => {}
let mounted = false

onMounted(async () => {
  mounted = true
  let renderer: WebGLRenderer | undefined
  try {
    const THREE = await import('three')
    const host = stage.value
    if (!mounted || !host) return

    renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true })
    const canvas = renderer.domElement
    const scene = new THREE.Scene()
    const camera = new THREE.OrthographicCamera(-1.58, 1.58, 1.83, -1.83, .1, 30)
    camera.position.set(0, 1.05, 8)
    camera.lookAt(0, .07, 0)
    scene.add(new THREE.HemisphereLight(0xf1faff, 0x8894c4, 2.1))
    const key = new THREE.DirectionalLight(0xfff2dc, 3.2)
    key.position.set(-3, 5, 4)
    scene.add(key)
    const rim = new THREE.DirectionalLight(0x77bfff, 2.3)
    rim.position.set(3, 2, -4)
    scene.add(rim)

    const penguin = new THREE.Group()
    scene.add(penguin)
    const sphere = new THREE.SphereGeometry(1, 32, 20)
    const navy = new THREE.MeshStandardMaterial({ color: 0x203552, roughness: .43 })
    const cream = new THREE.MeshStandardMaterial({ color: 0xfff5e5, roughness: .59 })
    const gold = new THREE.MeshStandardMaterial({ color: 0xf78c2b, roughness: .45 })
    const dark = new THREE.MeshStandardMaterial({ color: 0x10182d, roughness: .23 })
    const mint = new THREE.MeshStandardMaterial({ color: 0x23aaa3, roughness: .5 })
    const blush = new THREE.MeshStandardMaterial({ color: 0xf38774, roughness: .64 })

    function ellipsoid(parent: Group, material: typeof navy,
      position: [number, number, number], scale: [number, number, number]) {
      const mesh = new THREE.Mesh(sphere, material)
      mesh.position.set(...position)
      mesh.scale.set(...scale)
      parent.add(mesh)
      return mesh
    }

    ellipsoid(penguin, navy, [0, -.38, 0], [.94, 1.03, .68])
    ellipsoid(penguin, cream, [0, -.46, .40], [.68, .78, .34])
    const head = new THREE.Group()
    head.position.y = .61
    penguin.add(head)
    ellipsoid(head, navy, [0, .07, .01], [.79, .73, .64])
    const tuft = ellipsoid(head, navy, [-.11, .73, -.06], [.21, .25, .15])
    tuft.rotation.z = -.43
    const eyes = [-1, 1].map(side => {
      const patch = ellipsoid(head, cream, [side * .287, .012, .46], [.35, .42, .22])
      patch.rotation.z = side * -.22
      const eye = new THREE.Group()
      eye.position.set(side * .276, .112, .679)
      head.add(eye)
      ellipsoid(eye, dark, [0, 0, 0], [.105, .139, .068])
      ellipsoid(eye, cream, [-.027, .045, .057], [.034, .036, .02])
      ellipsoid(head, blush, [side * .43, -.125, .631], [.109, .044, .033])
      const foot = ellipsoid(penguin, gold, [side * .38, -1.36, .34], [.36, .17, .44])
      foot.rotation.y = side * .22
      return eye
    })
    ellipsoid(head, gold, [0, -.082, .74], [.226, .112, .243])
    const lowerBeak = ellipsoid(head, gold, [0, -.16, .726], [.17, .057, .174])

    const wings = [-1, 1].map(side => {
      const wing = new THREE.Group()
      wing.position.set(side * .76, -.04, -.03)
      ellipsoid(wing, navy, [side * .13, -.37, .025], [.23, .57, .185])
      penguin.add(wing)
      return wing
    })
    const scarf = new THREE.Mesh(new THREE.TorusGeometry(.63, .12, 12, 44), mint)
    scarf.rotation.x = Math.PI / 2 - .16
    scarf.scale.y = .81
    scarf.position.set(0, .053, .02)
    penguin.add(scarf)
    ellipsoid(penguin, mint, [.43, .024, .55], [.2, .14, .107])
    const scarfEnd = ellipsoid(penguin, mint, [.44, -.19, .59], [.145, .31, .105])
    scarfEnd.rotation.z = .22

    const motion = window.matchMedia('(prefers-reduced-motion: reduce)')
    let frame = 0
    let pointerX = 0
    let hovering = false
    let lastDraw = 0
    let visible = true
    const context = renderer
    context.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
    context.toneMapping = THREE.ACESFilmicToneMapping
    context.toneMappingExposure = 1.04

    const draw = (time: number) => {
      const animated = !motion.matches
      const t = animated ? time / 1000 : 0
      penguin.rotation.y = -.19 + pointerX * .38 + Math.sin(t * 1.55) * .075
      penguin.rotation.z = Math.sin(t * 1.65) * .035
      penguin.position.y = animated ? Math.sin(t * 2.6) * .065 : 0
      head.rotation.z = props.thinking && animated ? Math.sin(t * 4.8) * .12 : -.055
      const blink = animated ? 1 - .91 * Math.max(0, 1 - Math.abs((t % 4.6) - 3.5) / .12) : 1
      for (const eye of eyes) eye.scale.y = blink
      const wave = animated && (hovering || props.thinking || props.speaking)
      wings[0].rotation.z = -.28 - (wave ? .26 + Math.sin(t * 9) * .21 : 0)
      wings[1].rotation.z = .28 + (wave ? .74 + Math.sin(t * 9 + .5) * .30 : 0)
      lowerBeak.position.y = -.16 - (animated && props.speaking ? .08 * Math.abs(Math.sin(t * 17)) : 0)
      context.render(scene, camera)
    }
    const resize = () => {
      const { width, height } = host.getBoundingClientRect()
      if (width && height) {
        context.setSize(width, height)
        draw(performance.now())
      }
    }
    const tick = (time: number) => {
      if (visible && !document.hidden && !motion.matches && time - lastDraw >= 30) {
        lastDraw = time
        draw(time)
      }
      frame = requestAnimationFrame(tick)
    }
    const target = host.parentElement
    const move = (event: PointerEvent) => {
      const box = host.getBoundingClientRect()
      pointerX = box.width ? Math.max(-1, Math.min(1, 2 * (event.clientX - box.left) / box.width - 1)) : 0
      hovering = true
    }
    const leave = () => { pointerX = 0; hovering = false }
    const onMotion = () => draw(performance.now())
    const contextLost = (event: Event) => { event.preventDefault(); ready.value = false }
    const contextRestored = () => { ready.value = true; resize() }
    const sizeObserver = new ResizeObserver(resize)
    const visibilityObserver = new IntersectionObserver(([entry]) => { visible = !!entry?.isIntersecting })
    dispose = () => {
      cancelAnimationFrame(frame)
      sizeObserver.disconnect()
      visibilityObserver.disconnect()
      motion.removeEventListener('change', onMotion)
      target?.removeEventListener('pointermove', move)
      target?.removeEventListener('pointerleave', leave)
      canvas.removeEventListener('webglcontextlost', contextLost)
      canvas.removeEventListener('webglcontextrestored', contextRestored)
      sphere.dispose()
      scarf.geometry.dispose()
      for (const material of [navy, cream, gold, dark, mint, blush]) material.dispose()
      context.dispose()
      canvas.remove()
    }
    host.append(canvas)
    resize()
    draw(0)
    emit('portrait', canvas.toDataURL('image/png'))
    ready.value = true
    sizeObserver.observe(host)
    visibilityObserver.observe(host)
    motion.addEventListener('change', onMotion)
    target?.addEventListener('pointermove', move)
    target?.addEventListener('pointerleave', leave)
    canvas.addEventListener('webglcontextlost', contextLost)
    canvas.addEventListener('webglcontextrestored', contextRestored)
    frame = requestAnimationFrame(tick)
  } catch {
    dispose()
    renderer?.dispose()
  }
})

onBeforeUnmount(() => {
  mounted = false
  dispose()
})
</script>

<template>
  <span ref="stage" class="penny-3d" :class="{ 'penny-3d--ready': ready }" aria-hidden="true">
    <span v-if="!ready" class="penny-fallback"><slot /></span>
  </span>
</template>

<style scoped>
.penny-3d { position: relative; display: block; width: 100%; height: 100%; pointer-events: none; }
.penny-3d :deep(canvas) { display: block; visibility: hidden; }
.penny-3d--ready :deep(canvas) { visibility: visible; }
.penny-fallback { position: absolute; inset: 0; }
</style>
