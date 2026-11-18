# 0.4.13

修一个会让**输入框里的图片/文件预览完全消失**的客户端缺陷。本版只改客户端半边
（`client.js`），内核代码未变。

## 现象

把图片贴进 DSH 输入框后：附件其实已经上传成功（`~/.dsh/attachments/v1/objects/`
里能查到对应对象），但输入框里**什么都不显示**——没有缩略图、没有文件名 chip、
也没有可删除的 ×。

## 根因

`client.js` 为了在输入框里显示「AI 可操作范围」的窗口 chips，抢占了 DSH 的
`conversation.input.attachments` 插槽。这个 key 的声明是：

```ts
'conversation.input.attachments': { kind: 'single', scope: 'session-maybe' }
```

`single` 意味着**只有一个组件能占这一格**。插件的注册带着 `priority: -1000`
（数字越小越优先），于是把官方的 `ComposerAttachments` 顶掉了，而官方组件正是
渲染草稿缩略图、上传重试状态和拖放目标的那一个。

插件原本打算把官方组件"嵌回来"：

```js
const Official = entries.find((entry) => entry.registrant === officialOwner)?.component
```

但 `StoredEntry` 的真实形状是

```ts
interface StoredEntry {
  component: unknown
  options: { key?: string; id?: string; order?: number; label?: SlotLabel; priority?: number }
  select?; inject?; children?; store?
}
```

**没有 `registrant` 字段**，而且官方那条注册本身也没写 `registrant`。所以查找恒为
`undefined`，`Official` 永远是 `null`，附件区渲染成空。

## 修复

chips 改为注册进 `conversation.input.overlay`——这是给输入框上方浮层用的
**list** 插槽（`kind: 'list'`，官方命令面板也注册在这里），多个插件可以共存，
而且完全不碰附件插槽：

- 附件插槽还原给官方组件，缩略图/重试/拖放全部恢复；
- chips 仍然显示，浮在输入框上方；
- chips 容器 `pointer-events: none`，只有 chip 本体接指针事件 —— 即使浮在输入框
  上方，也不会挡住下面的输入区和附件区。

## 用例

`test/companion-client.test.js` 重写为 5 条，其中一条是硬回归：

> Client never registers into conversation.input.attachments

任何后来者再把注册名改回附件插槽都会立刻失败。

```
node --test test/companion-client.test.js   # 5 passed
```

## 升级后

改动在客户端的插件模块里，**需要重启 DSH**（或让渲染进程重新加载）才生效。
