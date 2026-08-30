# IPC-CTFAgent当前问题

1. 前端显示
   - 界面有点丑，优化想法未定
   - 显式显示思考过程与结果
   - 右边一大串文本简化（我也看不懂是什么）
   - 这个链子上面显示的，除了flag和goal这些明确内容，看不懂其他中间过程的内容，感觉不直观
2. JSON问题——思考模型返回思考内容处理（和前端思考显示可以联合做）
   - 典型输出：Member action failed for i001; retry 2 in 30s. model did not return a valid JSON action after 2 attempt(s): ValueError: no JSON action found in model output (finish_reason=length, content_length=9502)
3. 模型容易空转（这是黑盒，且和具体情况相关，得测试出原因）
4. CTF平台流程方面
   - 平台侧题目信息拉取适配——已做，需要检查
   - 平台侧提交flag失败反馈，agent调整思路重新做题
   - 平台侧判题返回正确，agent才应判断正确，而非模型认为正确就是正确
   - 对于flag做fakeflag和leet是否可读的自动识别，分别对应fakeflag和flag获取丢失的情况（这个现代llm应该都训练过）
   - 平台侧容器实例处理：最多容器并发数和容器题先后策略
   - 平台题目做题先后策略：和容器策略并行考虑，目前是优先解数多的题目
   - 拉取题目前先决定能否做，是否拉取——适用于moectf，但正常比赛一般无需这样
5. 不同平台的apikey适配
   - 做好不同接口，即时切换
   - 不同的key也决定并发数：plan和白嫖的key较少，走api可并发数较多，做好对应的适配
   - 额度不足时的告警（server酱，之前用的不错）/自动切换
6. AI（glm5.3flash）作为外部操作者使用时，经常出现SQlite锁卡死的情况，不知道是模型智商问题还是程序设计问题
7. wsrx开始出现连接问题，已解决（[wsrx-mcp](https://github.com/springbot2025/wsrx-mcp/blob)适配）
8. 待补充。。。





















