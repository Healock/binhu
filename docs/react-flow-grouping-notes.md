# React Flow 分组与任务区域约束调研

> 调研日期：2026-08-17
>
> 当前依赖：`@xyflow/react 12.11.3`
> 目标页面：`/task-flow-lab`

## 现状问题

当前“现在可做、等待协作、需要关注”只是画布背景上的三个视觉区域。任务卡仍是普通顶层节点，React Flow 不知道它们属于哪个区域，因此用户可以把任意任务拖到其他区域，区域标题与任务真实状态会失去对应关系。

这不是 CSS 间距或背景色问题，需要把区域升级为 React Flow 的父节点，并把任务卡变为父节点内部的子节点。

## React Flow 官方能力

### 1. `parentId`

节点设置 `parentId` 后，会成为对应父节点的子节点。子节点坐标改为相对于父节点左上角计算；移动父节点时，子节点会一起移动。

注意：父节点必须出现在 `nodes` 数组中所有子节点之前，否则 React Flow 可能无法正确计算父子关系。

### 2. `type: 'group'`

React Flow 提供内置的 `group` 节点类型。它默认不显示连接端口，适合当作区域、泳道或子流程容器。也可以注册自定义父节点，以展示标题、数量和说明。

### 3. `extent: 'parent'`

只设置 `parentId` 并不会阻止子节点拖出父节点。子节点还需要设置：

```ts
extent: 'parent'
```

这样任务卡只能在父节点边界内移动，不能再随手拖进其他区域。

### 4. `expandParent`

子节点设置 `expandParent: true` 后，拖到父节点边缘时会自动扩大父节点。本项目不建议默认使用，因为三个状态区域应保持相同宽度和统一高度，不能被单张卡片随意撑开。

推荐使用：

```ts
expandParent: false
```

区域高度由任务数量和自动布局统一计算。

### 5. `nodeExtent`

`<ReactFlow nodeExtent={...} />` 可以限制所有节点在同一个全局矩形范围内移动，但不能分别限制“现在可做、等待协作、需要关注”三个区域，所以不能替代父子分组。

### 6. `translateExtent`

`translateExtent` 限制的是画布视口的平移范围，不是单张任务卡所属的业务区域。它可以防止用户把整个画布拖到很远，但不能解决任务跨区问题。

### 7. 动态加入或离开分组

React Flow 可以在节点拖到某个分组上方时动态更新 `parentId`，并把绝对坐标换算成父节点相对坐标。官方“Parent Child Relation”示例展示了这种交互，但完整实现属于 React Flow Pro 示例。

这项能力技术上可以自行实现，但本项目不应默认把“拖动位置”解释为“改变业务状态”。任务属于哪个区域应由后端任务状态决定，而不是用户把卡片放在哪里决定。

## 项目推荐方案

### 方案结论

把三个状态区域实现为固定父节点，任务卡作为子节点，并使用 `extent: 'parent'` 锁定在所属区域内。

区域归属继续由系统计算：

| 区域 | 判断口径 |
|---|---|
| 现在可做 | 当前可以直接进入详情继续处理 |
| 等待协作 | 等待研判或其他岗位处理 |
| 需要关注 | 来源重复、冲突或待腾讯同步 |

用户只能调整任务在当前区域内的位置。任务真实状态变化后，页面自动修改它的 `parentId`，把卡片移动到新的区域。

### 父节点示例

```ts
const laneNodes = [
  {
    id: 'lane-ready',
    type: 'group',
    position: { x: 0, y: 0 },
    style: { width: 340, height: laneHeight },
    draggable: false,
    selectable: false,
    connectable: false,
    deletable: false,
    data: { lane: 'ready', label: '现在可做' },
  },
  {
    id: 'lane-waiting',
    type: 'group',
    position: { x: 380, y: 0 },
    style: { width: 340, height: laneHeight },
    draggable: false,
    selectable: false,
    connectable: false,
    deletable: false,
    data: { lane: 'waiting', label: '等待协作' },
  },
  {
    id: 'lane-exception',
    type: 'group',
    position: { x: 760, y: 0 },
    style: { width: 340, height: laneHeight },
    draggable: false,
    selectable: false,
    connectable: false,
    deletable: false,
    data: { lane: 'exception', label: '需要关注' },
  },
]
```

### 子节点示例

```ts
const taskNode = {
  id: taskFlowNodeId(task),
  type: 'task',
  parentId: `lane-${taskFlowLane(task)}`,
  extent: 'parent',
  expandParent: false,
  position: { x: 20, y: 64 + index * 196 },
  data: { task },
}
```

最终数组必须先放父节点：

```ts
const nodes = [...laneNodes, ...taskNodes]
```

## 区域尺寸和大量任务

如果一个核查人有几十条任务，固定高度会造成卡片重叠或没有可放置空间。推荐按三个区域中任务最多的一列统一计算高度：

```ts
const maxCount = Math.max(readyCount, waitingCount, exceptionCount)
const laneHeight = Math.max(560, 80 + maxCount * 196)
```

三个父节点使用相同高度，画布通过平移和缩放浏览。后续任务量进一步增长时，再增加“任务组折叠”或“每个业务类型一个堆叠节点”，而不是无限增大单张画布。

## 布局持久化变化

当前浏览器保存的是任务绝对坐标。改成父子分组后，应保存：

```ts
{
  nodeId: string,
  lane: 'ready' | 'waiting' | 'exception',
  relativePosition: { x: number, y: number }
}
```

恢复布局时必须校验保存的 `lane` 是否仍等于系统当前计算的区域：

- 相同：恢复用户在区域内的位置，并把坐标限制在父节点边界内。
- 不同：说明任务状态已经变化，忽略旧位置，在新区域中自动找空位。

手工虚线可以继续连接不同区域的任务，但不能改变父子关系或任务业务状态。

## 是否允许拖动跨区

第一阶段建议不允许。

如果后续确实需要自由沙盒，可以增加两个明确模式：

- **任务状态模式（默认）**：父节点锁定，不能跨区；区域反映真实状态。
- **个人规划模式**：不使用状态区域，用户自由摆放；位置不表达业务含义。

不能在同一个模式里既告诉用户“区域代表真实状态”，又允许用户随意把任务拖到错误区域。

## 实施步骤

1. 新增三个不可拖动的父节点，并保证父节点排列在任务节点之前。
2. 任务节点增加 `parentId`、`extent: 'parent'` 和 `expandParent: false`。
3. 把任务坐标改为父节点相对坐标。
4. 统一按最大任务数计算三个区域高度。
5. 修改本地布局结构，保存区域和相对坐标。
6. 状态变化导致跨区时，忽略旧位置并自动放入新区域空位。
7. 补充拖动边界、自动刷新跨区、布局恢复和深浅色测试。

## 官方资料

- Sub Flows 指南：<https://reactflow.dev/learn/layouting/sub-flows>
- Sub Flow 示例：<https://reactflow.dev/examples/grouping/sub-flows>
- Node API（`parentId`、`extent`、`expandParent`）：<https://reactflow.dev/api-reference/types/node>
- ReactFlow API（`nodeExtent`、`translateExtent`）：<https://reactflow.dev/api-reference/react-flow>
- 动态 Parent Child Relation（Pro）：<https://reactflow.dev/examples/grouping/parent-child-relation>
- Selection Grouping（Pro）：<https://reactflow.dev/examples/grouping/selection-grouping>
