"""Explicit live acceptance: requires a running daemon and incurs model costs.
Never reads the OpenRouter key; only the local service bearer token is used.
Usage: python scripts/verify_live.py --confirm-model-cost --output-dir PATH
"""
from __future__ import annotations
import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import httpx
from openai import OpenAI
from websockets.sync.client import connect

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Harness Live Acceptance</title>
<body style="font:20px system-ui;max-width:840px;margin:80px auto;padding:24px;background:#f4f6f8;color:#17212f">
<h1>Harness Live Acceptance</h1><p>Local isolated CDP browser test. No personal browser data.</p>
<label>Message <input id="message" value="pending" style="font-size:20px"></label>
<button id="apply" style="font-size:20px" onclick="document.querySelector('#result').textContent=document.querySelector('#message').value">Apply</button>
<p id="result">waiting</p></body></html>'''.encode()
TERMINAL = {"completed", "incomplete", "failed", "cancelled", "timed_out", "interrupted", "unknown"}

class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(PAGE)
    def log_message(self, *args): pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-model-cost', action='store_true')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('HARNESS_HOME', '~/.harness')).expanduser())
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--resume-from', type=Path, help='Resume only CLI/auth/selection checks after verified model/browser stages; no model calls.')
    args=parser.parse_args()
    if not args.confirm_model_cost and args.resume_from is None:
        parser.error('Live model calls require --confirm-model-cost')
    output=args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    home=args.home.expanduser().resolve()
    token=(home/'secrets/local-token').read_text().strip()
    client=httpx.Client(base_url=args.url,headers={'Authorization':'Bearer '+token},timeout=10,trust_env=False)
    observations={'started_at':time.time(),'runs':[]}
    def save(): (output/'live-result.json').write_text(json.dumps(observations,ensure_ascii=False,indent=2)+'\n')
    def request(method,path,body=None,headers=None):
        r=client.request(method,path,json=body,headers=headers)
        assert r.status_code < 300, (path,r.status_code,r.text)
        return r.json()
    def wait(run):
        deadline=time.monotonic()+135
        while time.monotonic()<deadline:
            full=request('GET',run['status_url'])
            if full['status'] in TERMINAL:
                observations['runs'].append(full); save(); return full
            time.sleep(.1)
        raise TimeoutError('Run still nonterminal; no duplicate submission: '+run['run_id'])
    def native(prompt,plugins,session=None):
        headers={'Idempotency-Key':str(uuid.uuid4())}
        if session: headers['X-Harness-Session-Id']=session
        return wait(request('POST','/api/runs',{'model':'harness-default','messages':[{'role':'user','content':prompt}], 'harness_plugins':plugins},headers))
    def tool(name,arguments,plugins):
        return wait(request('POST','/api/tools/call',{'name':name,'arguments':arguments,'harness_plugins':plugins}, {'Idempotency-Key':str(uuid.uuid4())}))
    def tool_results(run,name):
        return [event['data']['result'] for event in run['events'] if event['type']=='tool.result' and event['data']['name']==name]
    def finish_checks():
        cli=subprocess.run([sys.executable,'-m','harness','--home',str(home),'tools','call','arithmetic_parse',
                            '--args',json.dumps({'expression':'(35 × 7 - 128) ÷ (12 + 8)'},ensure_ascii=False),
                            '--plugin','arithmetic','--poll'],capture_output=True,text=True,timeout=20)
        (output/'cli-math.txt').write_text(cli.stdout+cli.stderr)
        assert cli.returncode==0 and '5.85' in cli.stdout,cli.stderr
        observations['cli_math_passed']=True
        observations['unauthorized_status']=httpx.get(args.url+'/api/status',trust_env=False).status_code
        assert observations['unauthorized_status']==401
        request('POST','/api/plugins/arithmetic/disable',{})
        default=request('GET','/api/tools')
        assert not any(t['name'].startswith('arithmetic_') for t in default)
        selected_bytes=(home/'config.yaml').read_bytes()
        temporary=tool('arithmetic_parse',{'expression':'2 + 3'},['arithmetic'])
        assert temporary['result']['result']['result']=='5'
        assert (home/'config.yaml').read_bytes()==selected_bytes
        assert not any(t['name'].startswith('arithmetic_') for t in request('GET','/api/tools'))
        request('POST','/api/plugins/arithmetic/enable',{'trust':True})
        observations['disable_and_override_passed']=True
        observations['passed']=True
    fixture=ThreadingHTTPServer(('127.0.0.1',0),PageHandler)
    threading.Thread(target=fixture.serve_forever,daemon=True).start()
    browser_session=None
    try:
        observations['service']=request('GET','/api/status')
        observations['model']=request('GET','/api/config')['model']
        assert observations['service']['ready'] and not observations['service']['busy']
        config_before=(home/'config.yaml').read_bytes()
        if args.resume_from is not None:
            previous=json.loads(args.resume_from.read_text())
            assert previous.get('sdk_nonstream') and previous.get('sdk_stream')
            assert previous.get('unreclaimed')==[] and previous.get('headed_window')
            assert previous['model']==observations['model']
            assert all(r['status']=='completed' for r in previous['runs'])
            observations.clear(); observations.update(previous)
            observations['previous_failure']=observations.pop('failure',None)
            observations['resumed_at']=time.time()
            finish_checks()
            print(json.dumps({'passed':True,'resumed_without_model_calls':True,'result_file':str(output/'live-result.json')},ensure_ascii=False))
            return
        with OpenAI(api_key=token,base_url=args.url+'/v1',max_retries=0,timeout=135,
                    http_client=httpx.Client(trust_env=False)) as sdk:
            raw=sdk.chat.completions.with_raw_response.create(model='harness-default',
                messages=[{'role':'user','content':'请只回复：SDK非流验收成功'}],extra_body={'harness_plugins':[]},
                extra_headers={'Idempotency-Key':str(uuid.uuid4())})
            value=raw.parse()
            assert 'SDK非流验收成功' in value.choices[0].message.content
            observations['sdk_nonstream']={'text':value.choices[0].message.content,'run_id':raw.headers['x-harness-run-id'],'usage':value.usage.model_dump() if value.usage else None}
            streamed=sdk.chat.completions.with_raw_response.create(model='harness-default',
                messages=[{'role':'user','content':'请只回复：SDK流验收成功'}],stream=True,
                stream_options={'include_usage':True},extra_body={'harness_plugins':[]},
                extra_headers={'Idempotency-Key':str(uuid.uuid4())})
            chunks=list(streamed.parse())
            text=''.join(c.choices[0].delta.content or '' for c in chunks if c.choices)
            assert 'SDK流验收成功' in text
            observations['sdk_stream']={'text':text,'run_id':streamed.headers['x-harness-run-id'], 'chunk_count':len(chunks), 'usage':next((c.usage.model_dump() for c in chunks if c.usage),None)}
        math=native('必须调用 arithmetic_parse 工具计算 (35 × 7 - 128) ÷ (12 + 8)，expression 必须原样输入。只在收到工具结果后给出答案，不心算、不用任何eval。',['arithmetic'])
        assert math['status']=='completed', math.get('error')
        assert any(r.get('result')=='5.85' for r in tool_results(math,'arithmetic_parse'))
        bad=tool('arithmetic_parse',{'expression':'(35 × 7 -) ÷ (12 + 8)'},['arithmetic'])
        assert bad['result']['result']['error']['code']=='syntax_error'
        observations['syntax_error']=bad['result']['result']['error']
        url=f'http://127.0.0.1:{fixture.server_port}/'
        script="document.querySelector('#message').value='Harness live CDP verified'; document.querySelector('#apply').click(); document.querySelector('#result').textContent"
        prompt=(f'这是已授权本地验收。第一轮调用 arithmetic_parse 计算 (35 × 7 - 128) ÷ (12 + 8)，同时调用 browser_open 创建一个 about:blank 自有测试页。'
                f'拿到target_id后，必须 browser_navigate 在该原tab导航到 {url}，不能再open。然后 browser_eval 编译执行以下原样JS：{script}。'
                '如果页面还未加载允许一次重试eval。接着 browser_extract max_chars=4000，再 browser_status 查看全浏览器快照。'
                '不要关闭页面，留给独立截图核验；不要release。最终简短报告算术工具结果和页面result文本，不心算。')
        browser=native(prompt,['arithmetic','browser-safety'])
        browser_session=browser['session_id']
        assert browser['status']=='completed', browser.get('error')
        for required in ['arithmetic_parse','browser_open','browser_navigate','browser_eval','browser_extract','browser_status']:
            assert tool_results(browser,required), ('missing real tool',required)
        assert any(r.get('result')=='5.85' for r in tool_results(browser,'arithmetic_parse'))
        extracts=tool_results(browser,'browser_extract')
        assert any('Harness live CDP verified' in r.get('text','') for r in extracts)
        target=tool_results(browser,'browser_open')[0]['target_id']
        runtime=json.loads((home/'plugin-data/browser-safety/runtime.json').read_text())
        observations['browser_instance_id']=runtime['instance_id']
        with connect(runtime['endpoint'],proxy=None,open_timeout=10) as connection:
            seq=0
            def cdp(method,params=None,session_id=None):
                nonlocal seq
                seq+=1; req={'id':seq,'method':method,'params':params or {}}
                if session_id: req['sessionId']=session_id
                connection.send(json.dumps(req))
                while True:
                    reply=json.loads(connection.recv(timeout=10))
                    if reply.get('id')==seq:
                        assert 'error' not in reply,reply
                        return reply.get('result',{})
            window=cdp('Browser.getWindowForTarget',{'targetId':target})
            observations['headed_window']=window
            assert window['bounds']['windowState']=='normal'
            cdp('Target.activateTarget',{'targetId':target})
            sid=cdp('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
            screenshot=cdp('Page.captureScreenshot',{'format':'png'},sid)
            (output/'browser-live.png').write_bytes(base64.b64decode(screenshot['data']))
        cleanup=native(f'现在独立截图核验已结束。只调用 browser_close 关闭我们刚才的target_id={target}，确认closed:true；再调用 browser_release。汇报 unreclaimed，不能关闭foreign页。',['browser-safety'],browser_session)
        assert cleanup['status']=='completed',cleanup.get('error')
        assert any(r.get('closed') is True for r in tool_results(cleanup,'browser_close'))
        assert tool_results(cleanup,'browser_release')[-1]['unreclaimed']==[]
        observations['unreclaimed']=[]
        assert (home/'config.yaml').read_bytes()==config_before, 'One-shot selection changed permanent config'
        observations['one_shot_config_unchanged']=True
        # Verify real CLI client entry, direct tool path, and no-plugin override without another model call.
        finish_checks()
    except BaseException as exc:
        observations['passed']=False
        observations['failure']={'type':type(exc).__name__,'message':str(exc)[:2000],'browser_session':browser_session}
        raise
    finally:
        observations['finished_at']=time.time(); save()
        fixture.shutdown(); fixture.server_close(); client.close()
    print(json.dumps({'passed':observations['passed'],'runs':[{'run_id':r['run_id'],'status':r['status'],'tool_calls':r['tool_calls']} for r in observations['runs']], 'headed_window':observations.get('headed_window'),'unreclaimed':observations.get('unreclaimed'),'result_file':str(output/'live-result.json')},ensure_ascii=False))

if __name__=='__main__': main()
