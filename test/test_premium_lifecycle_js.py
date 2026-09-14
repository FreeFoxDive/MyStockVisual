"""执行真实前端函数和虚拟时钟，验证延迟完成、刷新与换股取消。"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from js_test_util import require_node, run_node


class FrontendLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')

    def functions(self, names):
        return '\n'.join(re.search(r'^(?:async )?function ' + name + r'\(.*?\n\}',
                                   self.src, re.M | re.S).group() for name in names)

    def run_premium(self, body):
        harness = '''
let now=0, calls=0, nextId=0, timers=new Map();
Date.now=()=>now;
const setTimeout=(fn,ms)=>{const id=++nextId; timers.set(id,{fn,at:now+ms});return id;};
const clearTimeout=id=>timers.delete(id);
async function advance(ms) {
 const end=now+ms;
 for(let n=0;n<1000;n++) {
   const due=[...timers.entries()].filter(([id,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
   if(!due) {now=end;return;}
   now=due[1].at;timers.delete(due[0]);await due[1].fn();
 }
 throw Error('busy loop');
}
let document={hidden:false};
let STATE={symbol:'510300.SH',period:'1d',_fetchTs:1,premium:null,_premiumTask:null,
 klineData:{symbol:'510300.SH',period:'1d',meta:{deferred:['premium']},klines:[{date:'2026-09-14'}]}};
function updateInfo() {} function updateChart() {}
let fetch;
function ready(value=1, ttl=60) {return {ok:true,json:async()=>({premium:{ready:true,status:'ready',
 stale:false,dates:['2026-09-14'],values:[value],max_age_sec:ttl}})};}
'''
        js = harness + self.functions(['applyPremiumToKlines', 'reapplyPremium', 'premiumInMemory',
                                      'cancelPremium', 'fetchPremium', 'resumePremium'])
        js += '\n(async()=>{' + body + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        return json.loads(run_node(js))

    def test_slow_completion_after_old_retry_window(self):
        for ready_at in (7000, 15000, 30000):
            out = self.run_premium('''
fetch=async()=>{calls++;return now>=READY_AT ? ready(2) : {ok:true,json:async()=>({premium:{ready:false,status:'pending'}})};};
await fetchPremium(STATE.symbol,STATE.period,1006,1);
await advance(45000);
console.log(JSON.stringify({calls,value:STATE.klineData.klines[0].premium}));
'''.replace('READY_AT', str(ready_at)))
            self.assertGreater(out['calls'], 5)
            self.assertEqual(out['value'], 2)

    def test_expired_memory_requeries_and_updates(self):
        out = self.run_premium('''
fetch=async()=>ready(++calls,1);
await fetchPremium(STATE.symbol,STATE.period,1006,1);
await advance(2500);
console.log(JSON.stringify({calls,value:STATE.klineData.klines[0].premium}));
''')
        self.assertEqual(out['calls'], 3)
        self.assertEqual(out['value'], 3)

    def test_late_response_after_cancel_does_not_fill_new_symbol(self):
        out = self.run_premium('''
let resolve,signal;
fetch=async(url,opts)=>{signal=opts.signal;return await new Promise(r=>resolve=r);};
const work=fetchPremium(STATE.symbol,STATE.period,1006,1);
STATE.symbol='510050.SH';STATE._fetchTs=2;cancelPremium();
resolve(ready(9));await work;
console.log(JSON.stringify({aborted:signal.aborted,premium:STATE.premium,timers:timers.size}));
''')
        self.assertTrue(out['aborted'])
        self.assertIsNone(out['premium'])
        self.assertEqual(out['timers'], 0)

    def test_hidden_page_pauses_and_resumes(self):
        out = self.run_premium('''
fetch=async()=>ready(++calls);
document.hidden=true;
await fetchPremium(STATE.symbol,STATE.period,1006,1);
const hiddenCalls=calls;document.hidden=false;resumePremium();
for(let i=0;i<10;i++) await Promise.resolve();
console.log(JSON.stringify({hiddenCalls,calls}));
''')
        self.assertEqual(out, {'hiddenCalls': 0, 'calls': 1})

    def test_failure_obeys_negative_cache_retry(self):
        out = self.run_premium('''
fetch=async()=>{calls++;return {ok:true,json:async()=>({premium:{ready:false,status:'failed',retry_after_sec:300}})};};
await fetchPremium(STATE.symbol,STATE.period,1006,1);
await advance(299000);const before=calls;await advance(1000);
console.log(JSON.stringify({before,calls}));
''')
        self.assertEqual(out, {'before': 1, 'calls': 2})

    def test_new_date_and_count_invalidate_memory(self):
        out = self.run_premium('''
fetch=async()=>ready(++calls);
await fetchPremium(STATE.symbol,STATE.period,1006,1);
const same=premiumInMemory(STATE.symbol,STATE.period,1006);
const count=premiumInMemory(STATE.symbol,STATE.period,300);
STATE.klineData.klines.push({date:'2026-09-15'});
const newDay=premiumInMemory(STATE.symbol,STATE.period,1006);
console.log(JSON.stringify({same,count,newDay}));
''')
        self.assertEqual(out, {'same': True, 'count': False, 'newDay': False})

    def test_foreground_switch_same_millisecond_and_silent_refresh(self):
        js = '''
let requests=[],draws=[],hidden=0;
Date.now=()=>100;
let STATE={symbol:'A',period:'1d',adjust:'forward',_requestSeq:0,_foregroundLoading:false,
 _klineAbort:null,klineData:null,chart:{showLoading(){},hideLoading(){hidden++;}}};
const MINUTE_PERIODS=[],MINUTE_COUNTS={},DAILY_COUNT=1006;
const localStorage={getItem(){return null;},setItem(){}};
const element={style:{},classList:{add(){},remove(){}}};
const document={getElementById(){return element;}};
function C(){return {text:''};}
function cancelPremium(){} function syncAdjustMeta(){} function ensureQuoteStream(){}
function applyPremiumToKlines(){} function reconcileLiveBars(){} function _cleanLocalStorage(){}
function updateInfo(){} function addToHistory(){} function deriveSymbolType(){}
function fetchChips(){} function fetchPremium(){} function isChipPeriod(){return false;}
function renderFetchedKline(data){draws.push(data.symbol);}
async function loadTrades(){} function showToast(){throw Error('unexpected toast');}
function fetch(url,opts) {
 if(url.startsWith('/api/pledge'))return Promise.resolve({json:async()=>({})});
 return new Promise((resolve,reject)=>{
  requests.push({url,resolve});
  opts.signal.addEventListener('abort',()=>reject(Object.assign(new Error('abort'),{name:'AbortError'})));
 });
}
'''+ self.functions(['fetchData']) + '''
(async()=>{
const a=fetchData();await fetchData({silent:true});
const before=requests.length;
STATE.symbol='B';const b=fetchData();
requests[1].resolve({ok:true,json:async()=>({symbol:'B',period:'1d',klines:[]})});
await Promise.all([a,b]);
console.log(JSON.stringify({before,requestId:STATE._fetchTs,draws,hidden,loading:STATE._foregroundLoading}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        out = json.loads(run_node(js))
        self.assertEqual(out, {'before': 1, 'requestId': 2, 'draws': ['B'], 'hidden': 1, 'loading': False})


if __name__ == '__main__':
    unittest.main()
