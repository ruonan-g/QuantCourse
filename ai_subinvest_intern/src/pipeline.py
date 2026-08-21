"""
主观投研验证 pipeline：
Skill A: 想法拆解      → 可验证命题清单
Skill B: 命题查证      → 已验证/未验证命题 + 证据
Skill C: 逻辑链评估    → 逻辑强度评估（基于查证结果）
Skill D: 策略量化补全  → 量化参数 + 假设清单
Skill E: 回测生成      → 回测代码 + 执行结果（多套参数对比）
Skill F: 策略化输出    → Markdown 报告（结论 + 假设 + 敏感性）
"""

import json
import re
import os
import sys
import time
import subprocess
from  datetime import datetime
import pandas as pd
from utils.logger import setup_logger
import api_client as ac

TEMPERATURE_MAP = {
    "A_deidea": 0.1,
    "B1_logic": 0.3,
    "B2_timing": 0.3,
    "B3_value": 0.3,
    "B4_risk": 0.3,
    "B5_alternative": 0.3,
    "C_evaluate": 0.3,
    "D_detassume": 0.3,
    "E_backtest": 0.3,
    "F_finreport": 0.5,
}

class IdeaPipeline:
    def __init__(self, model="deepseek", output_dir = None, full_history=False):
        if output_dir is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            output_dir = os.path.join(project_root, "pipeline_outputs")

        self.full_history = full_history
        self.history = []  # 存储多轮对话历史
        self.logger = setup_logger(
            log_dir=output_dir
        )  # 日志也放在 pipeline_outputs/ 下
        # 创建以时间戳命名的输出文件夹
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(output_dir, self.run_id)
        os.makedirs(self.output_dir, exist_ok=True)
        self.logger.info(f"输出目录：{self.output_dir}")
        self.step_counter = 0  # 步骤计数器

    # ======== 加载 skill ========
    def load_skill(self, skill_name):
        """加载 Skill 文档"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        skill_path = os.path.join(project_root, "skills", f"{skill_name}.md")
        with open(skill_path, "r", encoding="utf-8") as f:
            return f.read()
        
    # 提取步骤操作指令
    def _extract_instruction(self, skill_content):
        """从 Skill 文档中提取 ## 步骤指令"""
        match = re.search(r"##\s*\d*\.?\s*步骤指令\s*\n(.*?)(?=\n## |\Z)", skill_content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    # ======== 输出清洗 ========
    def _sanitize_output(self,text):
        """
        去除模型输出常见的格式噪音：
        - Markdown 代码块包裹
        - 开头的空行
        """
        if text is None:
            return ""
        text = text.strip()
        # 去掉```yaml 或 ```json 或 ```python 包裹
        if text.startswith("```"):
            # 找到第一个换行符，去掉代码块标记行
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline+1:]
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()

    # ==================== 代码执行 Bridge ====================
    def extract_python_code(self, llm_output):
        # 优先匹配 ```python ... ```
        pattern_py = r"```python\s*\n(.*?)```"
        matches = re.findall(pattern_py, llm_output, re.DOTALL)
        if matches:
            code = "\n\n".join(matches)
            self.logger.info(f"【代码提取】找到 {len(matches)} 个 python 代码块，共 {len(code)} 字符")
            return code

        # 其次匹配 ``` ... ```（无语言标记）
        pattern_plain = r"```\s*\n(.*?)```"
        matches = re.findall(pattern_plain, llm_output, re.DOTALL)
        if matches:
            code = "\n\n".join(matches)
            self.logger.info(f"【代码提取】找到 {len(matches)} 个无标记代码块，共 {len(code)} 字符")
            return code

        # 没有代码块，可能是纯代码输出
        if re.search(r"(?:^|\n)\s*(?:import\s|from\s+\w+\s+import|def\s+\w+|print\()", llm_output):
            self.logger.info("【代码提取】未找到 ```python``` 包裹，但输出疑似纯 Python 代码，将直接使用")
        else:
            self.logger.warning("【代码提取】未找到 ```python``` 代码块，且输出不像代码，将使用整个输出作为代码")
        return llm_output

    def execute_generated_code(self, code, timeout=300):
        """
            执行生成的 Python 回测代码，捕获真实输出。

            参数:
                code: Python 代码字符串
                timeout: 最大执行时间（秒），默认 300 秒

            返回:
                dict: {
                    'code': 代码原文,
                    'status': 'success' | 'error' | 'timeout',
                    'stdout': 标准输出,
                    'stderr': 错误输出,
                    'returncode': 进程返回码,
                    'execution_time': 执行耗时（秒）
                }
        """
        self.logger.info(f"【代码执行】开始执行 | 超时设置: {timeout}s")

        # 获取项目根目录（src/ 的上级目录），确保 data/ 路径可用
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        data_dir = os.path.join(project_root, "data")

        # 保存代码到文件
        self.step_counter += 1
        step_dir = os.path.join(self.output_dir, f"step{self.step_counter:02d}_code_execution")
        os.makedirs(step_dir, exist_ok=True)
        code_file = os.path.join(step_dir, "backtest_script.py")
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(code)

        # 用当前 Python 解释器执行
        start_time = time.time()
        try:
            result = subprocess.run(
                [sys.executable, code_file],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=project_root,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = time.time() - start_time
            status = "success" if result.returncode == 0 else "error"

            # 保存执行结果到文件
            with open(
                os.path.join(step_dir, "stdout.txt"), "w", encoding="utf-8"
            ) as f:
                f.write(result.stdout or "(无输出)")
            with open(
                os.path.join(step_dir, "stderr.txt"), "w", encoding="utf-8"
            ) as f:
                f.write(result.stderr or "(无错误)")
            with open(
                os.path.join(step_dir, "exec_meta.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(
                    {
                        "status": status,
                        "returncode": result.returncode,
                        "execution_time": round(elapsed, 2),
                        "timeout_seconds": timeout,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            self.logger.info(f"【代码执行】执行完成 | 状态: {status} | 耗时: {elapsed:.1f}s | 返回码: {result.returncode}")
            if status == "error":
                self.logger.error(f"【代码执行】stderr 前500字: {(result.stderr or '')[:500]}")
            else:
                self.logger.info(f"【代码执行】stdout 前500字: {(result.stdout or '')[:500]}")

            return {
                "code": code,
                "status": status,
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "returncode": result.returncode,
                "execution_time": round(elapsed, 2),
            }

        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            self.logger.error(f"【代码执行】执行超时 | 已运行 {elapsed:.1f}s > {timeout}s")
            # 保存超时信息
            with open(
                os.path.join(step_dir, "stderr.txt"), "w", encoding="utf-8"
            ) as f:
                f.write(f"执行超时，超过 {timeout} 秒")
            return {
                "code": code,
                "status": "timeout",
                "stdout": "",
                "stderr": f"执行超时，超过 {timeout} 秒。可能原因：数据量过大、死循环、或计算复杂度过高。",
                "returncode": -1,
                "execution_time": round(elapsed, 2),
            }
        except Exception as e:
            elapsed = time.time() - start_time
            self.logger.error(f"【代码执行】执行异常 | 错误: {str(e)}")
            return {
                "code": code,
                "status": "error",
                "stdout": "",
                "stderr": f"执行异常: {str(e)}",
                "returncode": -1,
                "execution_time": round(elapsed, 2),
            }

    # ======== 结果保存 ========
    def _save_result(self, skill_name, input_data, output_data, elapsed):
        """保存当前步骤的输入、输出和元信息"""
        self.step_counter += 1
        step_dir = os.path.join(
            self.output_dir, f"step{self.step_counter:02d}_{skill_name}"
        )
        os.makedirs(step_dir, exist_ok=True)
        # 保存输入
        with open(os.path.join(step_dir, "input.txt"), "w", encoding="utf-8") as f:
            f.write(str(input_data))
        # 保存输出
        with open(os.path.join(step_dir, "output.txt"), "w", encoding="utf-8") as f:
            f.write(str(output_data))
        # 保存元信息
        meta = {
            "skill_name": skill_name,
            "step_number": self.step_counter,
            "elapsed_seconds": round(elapsed, 2),
            "timestamp": datetime.now().isoformat(),
        }
        with open(os.path.join(step_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # ======== 正式执行步骤 ========
    def execute_step(self, skill_name, context_data="", temperature=None, model="deepseek"):
        """
        execute a single step (skill)

        Parameters
        ----------
        skill_name : str   Skill 文件名（不含 .md）
        context_data : str  当前步骤的输入数据
        temperature : float or None  随机程度，None 则使用默认映射
        model : str  模型名称，默认 deepseek

        Returns
        -------
        str 模型输出文本
        """
        if temperature is None:
            temperature = TEMPERATURE_MAP.get(skill_name, 0.3)
        # ==== 日志：步骤开始 ====
        self.logger.info(f"【{skill_name}】开始执行 | model={model} | temperature={temperature}")
        self.logger.info(f"【{skill_name}】输入摘要：{str(context_data)[:200]}...")
        start_time = time.time()
        try:
            # 1. 加载 skill
            skill_content = self.load_skill(skill_name)
            instruction = self._extract_instruction(skill_content)
            # 2. 构建消息列表
            messages = [{"role": "system", "content": skill_content}]
            # 3. 追加历史
            if self.full_history and self.history:
                messages.extend(self.history)
            elif not self.full_history and self.history:
                # 默认：只传最近一轮
                messages.append(self.history[-2]) # 上一步 user
                messages.append(self.history[-1]) # 上一步 assistant
            # 4. 构建User Prompt
            if instruction:
                user_prompt = f"{instruction}\n\n以下是上一步骤的输出结果，请以此作为输入数据进行处理：\n\n{context_data}"
            else:
                user_prompt = context_data
            messages.append({"role": "user", "content": user_prompt})
            # 5. 调用模型
            response = ac.call_llm(messages, temperature = temperature, model=model)
            # 6. 清洗输出
            response = self._sanitize_output(response)

            elapsed = time.time() - start_time
            # ==== 日志：成功 ====
            self.logger.info(f"【{skill_name}】执行成功 | 耗时：{elapsed:.2f}秒")
            self.logger.info(f"【{skill_name}】输出摘要：{str(response)[:200]}...")

            # 7. 保存结果
            self._save_result(skill_name, input_data=context_data, output_data=response, elapsed=elapsed)
            # 8. 更新历史
            self.history.append({"role": "user", "content": user_prompt})
            self.history.append({"role": "assistant", "content": response})

            return response
        except Exception as e:
            elapsed = time.time() - start_time
            self.logger.error(f"【{skill_name}】执行失败 | 耗时: {elapsed:.2f}秒 | 错误: {str(e)}")
            self._save_result(skill_name, context_data, f"ERROR: {e}", elapsed)
            raise
    
    # ======== 重置历史对话 ========
    def reset(self):
        """重置历史，用于新的分析任务"""
        self.history = []
        self.step_counter = 0
        self.run_id = datetime.now().strftime("%Y%m%d%H%M%S")
        self.output_dir = os.path.join(self.output_dir, self.run_id)
        os.makedirs(self.output_dir, exist_ok=True)
        self.logger.info(f"新任务开始，输出目录：{self.output_dir}")