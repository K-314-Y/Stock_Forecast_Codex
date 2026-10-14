"""Listing/JSON only; transport rejects every unplanned GET."""
import json
from pathlib import Path
import tempfile
import unittest
from providers.jp.jpx_manual import historical_direct as d

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT/'analysis/jpx_listing_diagnostic_20261009T120749Z/listing.body').read_bytes()
SESSION = '2026-10-05'


def response(body, url=d.LISTING, status=200, kind='text/html', **headers):
    return d.Response(url,status,{'Content-Type':kind,**headers},body)


def jr(rows, url='https://www.jpx.co.jp/data.json'):
    return response(json.dumps({'TableDatas':rows}).encode(),url,kind='application/json')


class Transport:
    def __init__(self, responses): self.responses=responses; self.calls=[]
    def get(self,url,*,limit):
        self.calls.append(url)
        value=self.responses.pop(0)
        if value.url != url: raise AssertionError('Unexpected GET '+url)
        return value


class Tests(unittest.TestCase):
    def isolated_root(self, temp):
        root = Path(temp) / 'root'
        (root / 'config').mkdir(parents=True)
        (root / 'config/jpx_daily_acquisition_config.json').write_bytes((ROOT / 'config/jpx_daily_acquisition_config.json').read_bytes())
        (root / 'analysis').mkdir()
        (root / 'analysis/calendar_validation_2026-09-12.json').write_bytes((ROOT / 'analysis/calendar_validation_2026-09-12.json').read_bytes())
        return root

    def test_discovery_and_no_hardcoded_endpoint(self):
        changed=HTML.replace(b'/automation/markets/statistics-equities/daily/json/',b'/other/observed/').replace(b'tsedaily_report_monthlylist.json',b'published-months.json').replace(b'tsedaily_report_${month}.json',b'listed_${month}.json')
        discovery=d.json_endpoints(response(changed))
        self.assertEqual(discovery['monthly_list_endpoint'],'https://www.jpx.co.jp/other/observed/published-months.json')
        url=d.monthly_json_url(jr([{'Month':'202610'}]),discovery,SESSION)
        self.assertEqual(url,'https://www.jpx.co.jp/other/observed/listed_202610.json')
        self.assertIsNone(d.monthly_json_url(jr([{'Month':'202609'}]),discovery,SESSION))

    def test_external_endpoint(self):
        with self.assertRaisesRegex(ValueError,'NON_OFFICIAL'):
            d.json_endpoints(response(HTML.replace(b'/automation/markets/statistics-equities/daily/json/',b'https://evil.test/')))

    def test_missing_program(self):
        with self.assertRaisesRegex(ValueError,'DISCOVERY'):
            d.json_endpoints(response(HTML.replace(b'const BASE_PATH',b'const OTHER_PATH')))

    def test_exact_stocks_not_date_generated(self):
        link=d.json_stock_link(jr([{'TradeDate':'20261005','Stocks':'/published/unrelated-A17.pdf'}]),SESSION)
        self.assertEqual(link['resolved_pdf_url'],'https://www.jpx.co.jp/published/unrelated-A17.pdf')
        self.assertEqual(link['stocks_raw'],'/published/unrelated-A17.pdf')

    def test_absent_and_empty(self):
        for rows in ([],[{'TradeDate':'20261006','Stocks':'/a.pdf'}],*[ [{'TradeDate':'20261005','Stocks':v}] for v in ('',None,'-')]):
            self.assertIsNone(d.json_stock_link(jr(rows),SESSION))

    def test_duplicate_target(self):
        with self.assertRaisesRegex(ValueError,'AMBIGUOUS'):
            d.json_stock_link(jr([{'TradeDate':'20261005','Stocks':'/a.pdf'}]*2),SESSION)

    def test_json_decode_schema_and_content_type(self):
        for body in (b'{',b'{}',b'{"TableDatas":{}}',b'{"TableDatas":[],"TableDatas":[]}'):
            with self.assertRaises(ValueError): d._json_response(response(body,kind='application/json'))
        with self.assertRaises(ValueError): d._json_response(response(b'{"TableDatas":[]}'))
        for row in ({},{'TradeDate':'20261005'},{'TradeDate':'20260230','Stocks':'a'},{'TradeDate':'20261005','Stocks':1}):
            with self.assertRaises(ValueError): d.json_stock_link(jr([row]),SESSION)
        with self.assertRaises(ValueError): d.monthly_json_url(jr([{'Month':'bad'}]),d.json_endpoints(response(HTML)),SESSION)

    def test_observed_jpx_binary_json_type(self):
        value=response(b'{"TableDatas":[]}',url='https://www.jpx.co.jp/list.json',kind='binary/octet-stream')
        self.assertEqual(d._json_response(value),[])
        with self.assertRaises(ValueError): d._json_response(response(value.body,url='https://www.jpx.co.jp/list.html',kind='binary/octet-stream'))

    def test_status_challenge_redirect(self):
        for status in (403,429):
            with self.assertRaises(ValueError): d._json_response(response(b'{}',status=status,kind='application/json'))
        with self.assertRaisesRegex(ValueError,'SECURITY_CHALLENGE'):
            d._json_response(response(b'<title>Access Denied</title>',kind='text/html'))
        t=Transport([response(b'',status=302,Location='https://evil.test/a')])
        with self.assertRaisesRegex(ValueError,'NON_OFFICIAL'): d._request(t,d.LISTING,10000,3)
        self.assertEqual(t.calls,[d.LISTING])

    def test_stocks_external_or_non_pdf(self):
        for value in ('https://evil.test/a.pdf','/a.html'):
            with self.assertRaises(ValueError): d.json_stock_link(jr([{'TradeDate':'20261005','Stocks':value}]),SESSION)

    def test_full_flow_no_pdf_get_and_create_only(self):
        discovery=d.json_endpoints(response(HTML)); listurl=discovery['monthly_list_endpoint']
        monthurl=d.monthly_json_url(jr([{'Month':'202610'}]),discovery,SESSION)
        t=Transport([response(HTML),jr([{'Month':'202610'}],listurl),jr([{'TradeDate':'20261005','Stocks':'/real/non-date-name.pdf'}],monthurl)])
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'evidence'
            root=self.isolated_root(temp)
            result=d.discover_json_one(root,SESSION,transport=t,evidence_folder=folder)
            self.assertEqual(result['status'],'HREF_DISCOVERED'); self.assertEqual(result['pdf_requests'],0)
            self.assertEqual(t.calls,[d.LISTING,listurl,monthurl])
            self.assertTrue((folder/'discovery.json').exists())
            with self.assertRaises(FileExistsError): d.discover_json_one(root,SESSION,transport=t,evidence_folder=folder)

    def test_saved_live_responses(self):
        prior=ROOT/'analysis/jpx_json_listing_only_20261009T121508Z'
        month=ROOT/'analysis/jpx_json_listing_only_resume_20261009T121612Z'
        def load(folder,label):
            meta=json.loads((folder/(label+'.json')).read_bytes())
            return d.Response(meta['url'],meta['status'],meta['headers'],(folder/(label+'.body')).read_bytes())
        discovery=d.json_endpoints(load(prior,'listing.http1'))
        url=d.monthly_json_url(load(prior,'monthly_list.http1'),discovery,SESSION)
        value=load(month,'monthly.http1')
        self.assertEqual(url,value.url)
        link=d.json_stock_link(value,SESSION)
        self.assertEqual(link['trade_date'],'20261005')
        self.assertTrue(link['stocks_raw'])
        self.assertEqual(link['stocks_raw'],link['exact_href'])

    def test_failed_response_body_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)/'evidence'; t=Transport([response(b'Access Denied',status=403)])
            with self.assertRaisesRegex(ValueError,'HTTP_403'): d.discover_json_one(self.isolated_root(temp),SESSION,transport=t,evidence_folder=folder)
            # Response evidence is now written by the central security boundary.
            self.assertEqual((folder/'request-0001.body').read_bytes(),b'Access Denied')
            metadata = d.core.read_frozen(folder/'request-0001.json')
            self.assertEqual(metadata['status'],403)
            self.assertEqual(metadata['response_sha256'],d.core.sha256(folder/'request-0001.body'))
            self.assertTrue((folder/'failure.json').exists())

if __name__=='__main__': unittest.main()
