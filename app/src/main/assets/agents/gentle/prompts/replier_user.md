目标角色的人设与说话要求：
{persona_prompt}

当前时间：{current_time}

当前心情：{mood_state}

用户印象：
{impression_text}

长期记忆：
{memory_text}{history_section}{thinking_section}{guidance_section}
Planner 不会替你写好回复正文——它只把上面的内心判断、已知结果（含借助 AI 智能体得到的结果）交给你。请你结合当前对话上下文，自己组织语言，直接生成一条最终可发送给用户的回复。

要求：
1. 只输出回复正文
2. 不要解释
3. 不要加引号、括号、前后缀或额外标记
4. 保持当前角色的人设和语气
5. 聊天记录仅作为上下文参考，不要机械复述

现在，你说：
