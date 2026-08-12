import type { FullConfig } from '@nekosu/maa-tools'

const config: FullConfig = {
  cwd: import.meta.dirname,
  maaVersion: 'latest',
  interfacePath: 'assets/interface.json',
  check: {
    override: {
      // MaaPE 编辑产生的 $__mpe_code 元数据
      'mpe-config': 'ignore',
      // 流水线大量节点 next 与 on_error 指向同一目标（幂等设计），MaaFramework 可正常运行
      'duplicate-next': 'ignore',
      // 存档{存档序号}.png 等动态图片模板（{变量} 运行时替换）
      'unknown-image': 'ignore',
      'dynamic-image': 'ignore'
    }
  }
}

export default config
