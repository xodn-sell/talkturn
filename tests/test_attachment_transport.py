import copy
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from talkturn import attachments,codex_rpc,reply_bridge

SESSION='11111111-1111-4111-8111-111111111111'


class AttachmentTransportTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name)
        self.image=self.root/'photo.png'; self.image.write_bytes(b'synthetic image')
        self.document=self.root/'report $(x); & notes.pdf'; self.document.write_bytes(b'synthetic document')

    def test_reader_stays_read_only_and_writer_never_falls_back_to_stdio(self):
        with self.assertRaisesRegex(ValueError,'Read-only'):
            codex_rpc.CodexReader().call('thread/queue/add',{})
        raw=Mock(); raw.connect.side_effect=OSError('no shared server')
        with patch.object(codex_rpc.socket,'AF_UNIX',1,create=True),patch.object(codex_rpc.socket,'socket',return_value=raw), \
             patch.object(codex_rpc.subprocess,'Popen') as spawn:
            with self.assertRaises(RuntimeError):
                with codex_rpc.CodexAttachmentWriter():
                    self.fail('must not connect')
            spawn.assert_not_called()

    def test_codex_native_images_preserve_identity_event_and_literal_file_paths(self):
        origin={'source':'codex','session_id':SESSION}; before=copy.deepcopy(origin)
        with patch.object(attachments,'CodexAttachmentWriter') as writer:
            client=writer.return_value.__enter__.return_value
            client.enqueue.return_value={'accepted':True,'session_id':SESSION}
            receipt=attachments.forward_attachments(origin,'설명해 줘',[self.image,self.document],'event-123')
        thread,inputs,event=client.enqueue.call_args.args
        self.assertEqual((thread,event),(SESSION,'event-123'))
        self.assertEqual(inputs[1],{'type':'localImage','path':str(self.image.resolve())})
        self.assertIn(json.dumps(str(self.document.resolve()),ensure_ascii=False),inputs[0]['text'])
        self.assertEqual(receipt['transport'],'native_image_and_local_reference')
        self.assertEqual(receipt['native_image_count'],1)
        self.assertEqual(origin,before)

    def test_queue_start_failure_keeps_accepted_receipt_without_requeue(self):
        client=codex_rpc.CodexAttachmentWriter(); client.shared=True; client.ws=Mock()
        with patch.object(client,'_request',side_effect=[{'thread':{'id':SESSION}},
                {'queuedSubmission':{'id':'queued-1'}},TimeoutError('start uncertain')]) as request:
            receipt=client.enqueue(SESSION,[{'type':'localImage','path':'C:/synthetic/photo.png'}],'event-123')
        self.assertTrue(receipt['accepted']); self.assertTrue(receipt['resume_warning'])
        self.assertEqual([call.args[0] for call in request.call_args_list],['thread/read','thread/queue/add','thread/queue/start'])
        self.assertEqual(request.call_args_list[1].args[1]['clientUserMessageId'],'event-123')
        self.assertEqual(request.call_args_list[2].args[1],{'threadId':SESSION,'queuedSubmissionId':'queued-1'})
        with patch.object(client,'_request',return_value={'thread':{'id':'different'}}) as request:
            with self.assertRaises(RuntimeError): client.enqueue(SESSION,[],'event-124')
            request.assert_called_once()

    def test_plain_files_and_claude_images_are_explicit_references_only(self):
        for source,path in (('codex',self.document),('claude',self.image)):
            with self.subTest(source=source),patch.object(reply_bridge,'forward',return_value={'accepted':True}) as forward, \
                 patch.object(attachments,'CodexAttachmentWriter') as writer:
                origin={'source':source,'session_id':SESSION}
                receipt=attachments.forward_attachments(origin,'읽어 줘',[path],'event-reference')
                self.assertEqual(forward.call_args.args[0],origin)
                self.assertIn(json.dumps(str(path.resolve()),ensure_ascii=False),forward.call_args.args[1])
                self.assertEqual(receipt['transport'],'local_reference')
                self.assertEqual(receipt['native_image_count'],0)
                self.assertIn('확인하지 않았',receipt['reference_note'])
                writer.assert_not_called()

    def test_network_device_relative_and_link_paths_are_rejected_before_resolution(self):
        invalid=[r'\\server\share\photo.png',r'\/server/share/photo.png',r'\\?\C:\photo.png',
                 r'\??\UNC\server\photo.png','file:///C:/photo.png',r'\photo.png','photo.png']
        with patch.object(Path,'resolve',side_effect=AssertionError('unsafe resolution')):
            for value in invalid:
                with self.subTest(path=value),self.assertRaises(ValueError): attachments._validated_paths([value])
            with patch.object(Path,'lstat',return_value=SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0x400)):
                with self.assertRaises(ValueError): attachments._validated_paths([self.image])

    def test_limits_and_known_remote_targets_fail_without_sending(self):
        with patch.object(reply_bridge,'forward') as forward,patch.object(attachments,'CodexAttachmentWriter') as writer:
            with self.assertRaises(ValueError): attachments._validated_paths([self.image]*9)
            with patch.object(attachments,'MAX_FILE_BYTES',1):
                with self.assertRaises(ValueError): attachments._validated_paths([self.image])
            with patch.object(attachments,'MAX_TOTAL_BYTES',1):
                with self.assertRaises(ValueError): attachments._validated_paths([self.image,self.document])
            with self.assertRaises(ValueError):
                attachments.forward_attachments({'source':'claude','session_id':SESSION,'client':'원격 Code'},'', [self.image],'event-remote')
            with self.assertRaises(ValueError):
                attachments.forward_attachments({'source':'codex','session_id':'a name'},'', [self.image],'event-id')
            forward.assert_not_called(); writer.assert_not_called()


if __name__=='__main__':
    unittest.main()
