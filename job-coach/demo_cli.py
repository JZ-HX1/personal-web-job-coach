"""独立虚构档案演示，不改用户在浏览器中的真实求职资料。"""
import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'runtime' / 'deps'))
from career.agent import ModelClient, load_config, run_agent
from career.service import Service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--business-only', action='store_true')
    parser.add_argument('--prompt', default='对比简历和岗位JD，给出匹配度和三个修改建议，再生成整套求职材料。')
    args = parser.parse_args()
    service = Service(ROOT / 'runtime' / 'cli-demo')
    session = uuid.uuid4().hex
    service.load_demo(session)
    if args.business_only:
        print('普通业务演练（不调用模型，不作为Tool Calling证据）')
        result = {'match': service.analyze_fit(session), 'resume': service.build_resume(session),
                  'interview': service.start_interview(session), 'exports': service.export_materials(session)}
    else:
        print('真实模型工具调用，以下为虚构教学示例。')
        result = run_agent(service, session, args.prompt, ModelClient(load_config(ROOT)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('演示文件目录：', service.root / 'exports')


if __name__ == '__main__':
    main()
