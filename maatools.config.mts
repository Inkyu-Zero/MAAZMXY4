import type { FullConfig } from '@nekosu/maa-tools'

const config: FullConfig = {
  cwd: import.meta.dirname,
  maaVersion: 'latest',
  interfacePath: 'assets/interface.json',
  check: {
    override: {
      // 忽略 mpe-config 带来的报错
      // ignore warning caused by mpe-config
      // 'mpe-config': 'ignore'
      // 流水线大量节点 next 与 on_error 指向同一目标（幂等设计），MaaFramework 可正常运行，降级为警告
      'duplicate-next': 'warning'
    }
  }
}

export default config
