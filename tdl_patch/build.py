"""Build-time, exact-source patch; no credentials or generated binaries in the repository."""
import hashlib
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

REV = '9d7d49eef2bcb04da720c26e33598c49c68b9ddd'
SHA = '072039b13548f6b6e7cc38cc11ddfaeddd310d5797f6af3c44714cb460489b62'
PATCH = Path(__file__).resolve().parent
root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
archive_path = Path(sys.argv[2]) if len(sys.argv) > 2 else PATCH / 'upstream' / 'tdl-0.20.4.tar.gz'
with tempfile.TemporaryDirectory() as tmp:
    archive = archive_path
    with archive.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != SHA:
            raise SystemExit('TDL source checksum mismatch')
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or '\\' in member.name
                    or not name.parts or name.parts[0] != 'tdl-' + REV
                    or not (member.isdir() or member.isfile())):
                raise SystemExit('Unexpected TDL source archive member')
            target = Path(tmp).joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
    source = Path(tmp) / ('tdl-' + REV)
    if any(root.iterdir()):
        raise SystemExit('TDL build directory must be empty')
    shutil.copytree(source, root, dirs_exist_ok=True)


def edit(file, before, after, count=1):
    path = root / file
    text = path.read_text('utf-8')
    if text.count(before) != count:
        raise SystemExit(f'Pinned TDL patch does not match: {file}: {before[:70]}')
    path.write_text(text.replace(before, after), encoding='utf-8')


edit('cmd/forward.go', '\treturn cmd\n', '''\tcmd.Flags().StringVar(&opts.RelayReceipt, "relay-receipt", "", "Web: API receipt file (exclusive create)")
\tcmd.Flags().StringVar(&opts.RelayToken, "relay-token", "", "Web: receipt correlation token")
\tcmd.Flags().IntSliceVar(&opts.RelayMembers, "relay-members", nil, "Web: exact source message IDs")
\tcmd.Flags().IntVar(&opts.RelayEditTarget, "relay-edit-target", 0, "Web: edit this existing target message only")
\tcmd.Flags().Int64Var(&opts.RelayActor, "relay-actor", 0, "Web: required original sending account")
\treturn cmd
''')
edit('app/forward/forward.go', '\tDesc   bool\n', '''\tDesc   bool
\tRelayReceipt string
\tRelayToken string
\tRelayMembers []int
\tRelayEditTarget int
\tRelayActor int64
''')
edit('app/forward/forward.go', '\tfw := forwarder.New(forwarder.Options{', '''\tvar relay *forwarder.Relay
\tif opts.RelayReceipt != "" {
\t\tif opts.Mode != forwarder.ModeClone || opts.DryRun || opts.Edit != "" || opts.RelayToken == "" || len(opts.RelayMembers) == 0 || len(opts.From) != 1 {
\t\t\treturn fmt.Errorf("invalid web relay options")
\t\t}
\t\tself, err := manager.Self(ctx)
\t\tif err != nil { return err }
\t\tif opts.RelayActor != 0 && opts.RelayActor != self.ID() { return fmt.Errorf("TDL account differs from original sender") }
\t\tfile, err := os.OpenFile(opts.RelayReceipt, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
\t\tif err != nil { return err }
\t\tdefer multierr.AppendInvoke(&rerr, multierr.Close(file))
\t\trelay = &forwarder.Relay{File: file, Token: opts.RelayToken, Actor: self.ID(), EditTarget: opts.RelayEditTarget, Members: opts.RelayMembers}
\t} else if opts.RelayEditTarget != 0 || opts.RelayActor != 0 || opts.RelayToken != "" || len(opts.RelayMembers) != 0 {
\t\treturn fmt.Errorf("web relay options require --relay-receipt")
\t}
\tfw := forwarder.New(forwarder.Options{
\t\tRelay: relay,''')
edit('core/forwarder/forwarder.go', '\tProgress Progress\n', '\tProgress Progress\n\tRelay *Relay\n')
# Strict mode must not swallow errors or partially degrade an album to text.
edit('core/forwarder/forwarder.go', '\t\t\tif err != nil {\n\t\t\t\tcontinue\n\t\t\t}',
     '\t\t\tif err != nil {\n\t\t\t\tif f.opts.Relay != nil { return err }\n\t\t\t\tcontinue\n\t\t\t}')
edit('core/forwarder/forwarder.go', '\t\t\tif err = f.forwardMessage(ctx, elem, grouped...); err != nil {\n',
     '\t\t\tif err = f.forwardMessage(ctx, elem, grouped...); err != nil {\n\t\t\t\tif f.opts.Relay != nil { return err }\n')
edit('core/forwarder/forwarder.go', '\t\tif err := f.forwardMessage(ctx, elem); err != nil {\n',
     '\t\tif err := f.forwardMessage(ctx, elem); err != nil {\n\t\t\tif f.opts.Relay != nil { return err }\n')
edit('core/forwarder/forwarder.go', '\tf.opts.Progress.OnAdd(elem)',
     '\tif f.opts.Relay != nil { if err := f.opts.Relay.Validate(elem, grouped); err != nil { return err } }\n\tf.opts.Progress.OnAdd(elem)')
edit('core/forwarder/forwarder.go', '\tswitch elem.Mode() {',
     '\tif f.opts.Relay != nil && f.opts.Relay.EditTarget != 0 { return f.relayEdit(ctx, elem, convForwardedMedia) }\n\tswitch elem.Mode() {')
edit('core/forwarder/forwarder.go', '(!protectedDialog(elem.From()) && !protectedMessage(msg)) || !photoOrDocument(msg.Media)',
     '(f.opts.Relay == nil && !protectedDialog(elem.From()) && !protectedMessage(msg)) || !photoOrDocument(msg.Media)')
edit('core/forwarder/forwarder.go', '\t\tif _, err := f.forwardClient(ctx, elem).MessagesSendMessage(ctx, req); err != nil {\n\t\t\treturn errors.Wrap(err, "send message")\n\t\t}\n\t\treturn nil',
     '\t\tupdates, err := f.forwardClient(ctx, elem).MessagesSendMessage(ctx, req)\n\t\tif err != nil { return errors.Wrap(err, "send message") }\n\t\treturn f.relaySent(elem, updates, []int{msg.ID}, []int64{req.RandomID})')
edit('core/forwarder/forwarder.go', '\t\t\t\t\tlog.Debug("Can\'t convert forwarded media", zap.Error(err))',
     '\t\t\t\t\tif f.opts.Relay != nil { return err }\n\t\t\t\t\tlog.Debug("Can\'t convert forwarded media", zap.Error(err))')
edit('core/forwarder/forwarder.go', '\t\t\t\tif _, err := f.forwardClient(ctx, elem).MessagesSendMultiMedia(ctx, req); err != nil {\n\t\t\t\t\treturn errors.Wrap(err, "send multi media")\n\t\t\t\t}\n\t\t\t\treturn nil',
     '''\t\t\t\tupdates, err := f.forwardClient(ctx, elem).MessagesSendMultiMedia(ctx, req)
\t\t\t\tif err != nil { return errors.Wrap(err, "send multi media") }
\t\t\t\tsources, random := make([]int, len(grouped)), make([]int64, len(media))
\t\t\t\tfor i, m := range grouped { sources[i] = m.ID }
\t\t\t\tfor i, m := range media { random[i] = m.RandomID }
\t\t\t\treturn f.relaySent(elem, updates, sources, random)''')
edit('core/forwarder/forwarder.go', '\n\t\t\tlog.Debug("Can\'t convert forwarded media", zap.Error(err))',
     '''
\t\t\tif f.opts.Relay != nil {
\t\t\t\tswitch elem.Msg().Media.(type) {
\t\t\t\tcase nil, *tg.MessageMediaWebPage:
\t\t\t\tdefault: return err
\t\t\t\t}
\t\t\t}
\t\t\tlog.Debug("Can't convert forwarded media", zap.Error(err))''')
edit('core/forwarder/forwarder.go', '\t\tif _, err := f.forwardClient(ctx, elem).MessagesSendMedia(ctx, req); err != nil {\n\t\t\treturn errors.Wrap(err, "send single media")\n\t\t}\n\t\treturn nil',
     '\t\tupdates, err := f.forwardClient(ctx, elem).MessagesSendMedia(ctx, req)\n\t\tif err != nil { return errors.Wrap(err, "send single media") }\n\t\treturn f.relaySent(elem, updates, []int{elem.Msg().ID}, []int64{req.RandomID})')
for name in ('relay.go', 'relay_test.go'):
    shutil.copy2(PATCH / name, root / 'core/forwarder' / name)
print('Applied verified TDL source patch:', REV)
