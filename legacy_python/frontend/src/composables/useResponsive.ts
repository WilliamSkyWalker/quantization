import { ref, onMounted, onUnmounted } from 'vue'

const MOBILE_BREAKPOINT = 768

const isMobile = ref(false)

function update() {
  isMobile.value = window.innerWidth < MOBILE_BREAKPOINT
}

let listeners = 0

export function useResponsive() {
  onMounted(() => {
    if (listeners === 0) {
      update()
      window.addEventListener('resize', update)
    }
    listeners++
  })

  onUnmounted(() => {
    listeners--
    if (listeners === 0) {
      window.removeEventListener('resize', update)
    }
  })

  return { isMobile }
}
