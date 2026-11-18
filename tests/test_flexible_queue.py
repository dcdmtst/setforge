import tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from setforge.workqueue import WorkQueue

class FlexibleQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.run=Path(self.tmp.name);self.q=WorkQueue(self.run)
        self.q.configure(artists=2,reviewers=2,workers=3)
    def job(self,n,role='image',card=None,kind=None,priority=0):
        kind=kind or ('image' if role=='image' else 'card_review');key=f'{n:04d}-{kind}'
        self.q.register({'id':key,'role':role,'task_key':kind+':'+(card or 'all'),'request_sha256':str(n).zfill(64)},card=card,kind=kind,priority=priority)
        return key
    def finish(self,lease):
        self.q.deliver(lease['id'],lease['worker'],lease['token'],{'fixture':True})
        self.q.acknowledge(lease['id'])
    def test_two_reviews_global_cap_and_cross_role_exclusion(self):
        self.job(1,'reviewer','s/a');self.job(2,'reviewer','s/b');self.job(3,'reviewer','s/c');self.job(4,card='s/d');self.job(5,card='s/e')
        self.assertIsNotNone(self.q.claim('chief','reviewer'))
        self.assertIsNotNone(self.q.claim('flex-a','reviewer'))
        self.assertIsNone(self.q.claim('third-reviewer','reviewer'))
        self.assertIsNone(self.q.claim('flex-a','image'))
        self.assertIsNotNone(self.q.claim('artist','image'))
        self.assertIsNone(self.q.claim('extra-artist','image'))
    def test_priority_precedes_affinity_fifo_and_is_mutable_only_while_ready(self):
        first=self.job(1,card='s/prop');source=self.job(2,card='s/source',priority=300)
        lease=self.q.claim('artist','image');self.assertEqual(lease['id'],source)
        self.q.set_priority(source,999);self.assertEqual(self.q.jobs()[1]['priority'],300)
        self.finish(lease);self.q.set_priority(first,400)
        self.assertEqual(self.q.claim('artist','image')['id'],first)
    def test_any_prior_artist_cannot_review_card_or_entity_after_artist_replacement(self):
        self.job(1,card='s/a');self.finish(self.q.claim('artist-a','image'))
        self.job(2,card='s/a');self.finish(self.q.claim('artist-b','image'))
        self.job(3,'reviewer','s/a');self.job(4,'reviewer','s/a','entity_review')
        self.assertIsNone(self.q.claim('artist-a','reviewer'))
        self.assertIsNone(self.q.claim('artist-b','reviewer'))
        self.assertIsNotNone(self.q.claim('chief','reviewer'))
    def test_flex_prefers_review_backlog_but_never_global_reviews(self):
        self.job(1,card='s/prop');self.job(2,'reviewer','s/a');self.job(3,'reviewer','s/b')
        self.assertEqual(self.q.claim('flex','flex')['role'],'reviewer')
        other=Path(self.tmp.name)/'other';other.mkdir();q=WorkQueue(other);q.configure(2,reviewers=2,workers=3)
        q.register({'id':'set','role':'reviewer','task_key':'set_review:set_review','request_sha256':'a'*64},kind='set_review')
        self.assertIsNone(q.claim('flex','flex'))
        self.assertIsNotNone(q.claim('chief','reviewer'))
    def test_artists_cannot_claim_global_review_by_switching_role(self):
        self.job(1,card='s/a');self.finish(self.q.claim('artist','image'))
        self.job(2,'reviewer',None,'set_review')
        self.assertIsNone(self.q.claim('artist','reviewer'))
        self.assertIsNotNone(self.q.claim('chief','reviewer'))

    def test_racing_flexible_claims_share_one_global_capacity(self):
        for n in range(1,13):self.job(n,'reviewer' if n%2 else 'image','s/'+str(n))
        with ThreadPoolExecutor(max_workers=12) as pool:
            leases=list(pool.map(lambda n:self.q.claim('flex-'+str(n),'flex'),range(12)))
        active=[x for x in leases if x]
        self.assertEqual(len(active),3)
        self.assertEqual(len({x['id'] for x in active}),3)
        self.assertLessEqual(sum(x['role']=='reviewer' for x in active),2)
        self.assertLessEqual(sum(x['role']=='image' for x in active),2)

if __name__=='__main__':unittest.main()
