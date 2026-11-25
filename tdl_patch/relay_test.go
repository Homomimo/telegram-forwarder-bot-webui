package forwarder

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/gotd/td/bin"
	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
)

func TestReceiptIDsUseRandomIDInsteadOfResponseOrder(t *testing.T) {
	updates := &tg.Updates{Updates: []tg.UpdateClass{
		&tg.UpdateMessageID{ID: 703, RandomID: 13},
		&tg.UpdateMessageID{ID: 701, RandomID: 11},
		&tg.UpdateMessageID{ID: 702, RandomID: 12},
	}}
	ids, err := receiptIDs(updates, []int64{11, 12, 13})
	if err != nil || !reflect.DeepEqual(ids, []int{701, 702, 703}) {
		t.Fatalf("%v %v", ids, err)
	}
}

func TestReceiptIDsRejectAmbiguousOrIncompleteResponses(t *testing.T) {
	for _, tc := range []struct {
		updates tg.UpdatesClass
		random  []int64
	}{
		{&tg.Updates{}, []int64{11}},
		{&tg.Updates{Updates: []tg.UpdateClass{&tg.UpdateMessageID{ID: 0, RandomID: 11}}}, []int64{11}},
		{&tg.Updates{Updates: []tg.UpdateClass{&tg.UpdateMessageID{ID: 71, RandomID: 11}, &tg.UpdateMessageID{ID: 71, RandomID: 12}}}, []int64{11, 12}},
		{&tg.Updates{Updates: []tg.UpdateClass{&tg.UpdateMessageID{ID: 71, RandomID: 11}, &tg.UpdateMessageID{ID: 72, RandomID: 11}}}, []int64{11}},
		{&tg.UpdateShortSentMessage{ID: 71}, []int64{11, 12}},
	} {
		if ids, err := receiptIDs(tc.updates, tc.random); err == nil {
			t.Fatalf("accepted ambiguous receipt %v", ids)
		}
	}
}

func TestReceiptIDsAcceptDirectShortAndCombinedResponses(t *testing.T) {
	for _, updates := range []tg.UpdatesClass{
		&tg.UpdateShortSentMessage{ID: 71},
		&tg.UpdateShort{Update: &tg.UpdateMessageID{ID: 71, RandomID: 11}},
		&tg.UpdatesCombined{Updates: []tg.UpdateClass{&tg.UpdateMessageID{ID: 71, RandomID: 11}}},
	} {
		ids, err := receiptIDs(updates, []int64{11})
		if err != nil || !reflect.DeepEqual(ids, []int{71}) {
			t.Fatalf("%v %v", ids, err)
		}
	}
}

func TestEmptyCaptionAndEntitiesAreExplicitlySent(t *testing.T) {
	req := &tg.MessagesEditMessageRequest{ID: 71}
	req.SetMessage("")
	req.SetEntities(nil)
	req.SetFlags()
	if message, ok := req.GetMessage(); !ok || message != "" {
		t.Fatal("caption clearing flag missing")
	}
	if entities, ok := req.GetEntities(); !ok || len(entities) != 0 {
		t.Fatal("entity clearing flag missing")
	}
}

type relayTestPool struct{ client *tg.Client }

func (p relayTestPool) Client(context.Context, int) *tg.Client  { return p.client }
func (p relayTestPool) Takeout(context.Context, int) *tg.Client { return p.client }
func (p relayTestPool) Default(context.Context) *tg.Client      { return p.client }
func (p relayTestPool) Close() error                            { return nil }

type relayTestInvoker func(context.Context, bin.Encoder, bin.Decoder) error

func (i relayTestInvoker) Invoke(ctx context.Context, in bin.Encoder, out bin.Decoder) error {
	return i(ctx, in, out)
}

type relayTestElem struct {
	from, to peers.Peer
	msg      *tg.Message
	grouped  bool
}

func (e relayTestElem) Mode() Mode       { return ModeClone }
func (e relayTestElem) From() peers.Peer { return e.from }
func (e relayTestElem) To() peers.Peer   { return e.to }
func (e relayTestElem) Msg() *tg.Message { return e.msg }
func (e relayTestElem) Thread() int      { return 0 }
func (e relayTestElem) AsSilent() bool   { return false }
func (e relayTestElem) AsDryRun() bool   { return false }
func (e relayTestElem) AsGrouped() bool  { return e.grouped }

func TestRelayEditNeverSendsNewMessageAndKeepsTargetID(t *testing.T) {
	for _, media := range []tg.InputMediaClass{nil, &tg.InputMediaPhoto{ID: &tg.InputPhoto{ID: 99}}} {
		editCalls := 0
		client := tg.NewClient(relayTestInvoker(func(_ context.Context, in bin.Encoder, out bin.Decoder) error {
			switch req := in.(type) {
			case *tg.MessagesGetHistoryRequest:
				out.(*tg.MessagesMessagesBox).Messages = &tg.MessagesMessages{Messages: []tg.MessageClass{&tg.Message{ID: 71, PeerID: &tg.PeerChannel{ChannelID: 22}}}}
			case *tg.MessagesEditMessageRequest:
				editCalls++
				if req.ID != 71 {
					t.Fatal("edited a different target")
				}
				if text, ok := req.GetMessage(); !ok || text != "" {
					t.Fatal("failed to clear caption")
				}
				if media != nil && req.Media != media {
					t.Fatal("replacement media missing")
				}
				out.(*tg.UpdatesBox).Updates = &tg.Updates{}
			default:
				return fmt.Errorf("unexpected RPC (must not send a new post): %T", in)
			}
			return nil
		}))
		manager := peers.Options{}.Build(client)
		msg := &tg.Message{ID: 42, PeerID: &tg.PeerChannel{ChannelID: 11}, Message: ""}
		if media != nil {
			msg.Media = &tg.MessageMediaPhoto{Photo: &tg.PhotoEmpty{ID: 99}}
		}
		elem := relayTestElem{from: manager.Channel(&tg.Channel{ID: 11}), to: manager.Channel(&tg.Channel{ID: 22}), msg: msg}
		path := filepath.Join(t.TempDir(), "receipt.jsonl")
		file, err := os.Create(path)
		if err != nil {
			t.Fatal(err)
		}
		r := &Relay{File: file, Token: "token", Actor: 777, EditTarget: 71, Members: []int{42}}
		f := New(Options{Pool: relayTestPool{client}, Relay: r})
		err = f.relayEdit(context.Background(), elem, func(*tg.Message) (tg.InputMediaClass, error) { return media, nil })
		file.Close()
		if err != nil || editCalls != 1 {
			t.Fatalf("calls=%d error=%v", editCalls, err)
		}
		data, _ := os.ReadFile(path)
		var receipt relayReceipt
		if err := json.Unmarshal(data, &receipt); err != nil || receipt.Target != 71 || receipt.Operation != "edit" || receipt.Source != 42 {
			t.Fatalf("bad receipt: %s", data)
		}
	}
}

func TestRelayRejectsChangedAlbumOrGroupedEditBeforeSending(t *testing.T) {
	manager := peers.Options{}.Build(tg.NewClient(relayTestInvoker(func(context.Context, bin.Encoder, bin.Decoder) error { t.Fatal("unexpected RPC"); return nil })))
	elem := relayTestElem{from: manager.Channel(&tg.Channel{ID: 11}), to: manager.Channel(&tg.Channel{ID: 22}), msg: &tg.Message{ID: 42}, grouped: true}
	r := &Relay{Members: []int{42, 43}}
	if r.Validate(elem, []*tg.Message{{ID: 42}, {ID: 44}}) == nil {
		t.Fatal("accepted changed album")
	}
	r.Members, r.EditTarget = []int{42}, 71
	if r.Validate(elem, nil) == nil {
		t.Fatal("accepted grouped edit that could recreate an album")
	}
}

func TestRelaySortsAlbumMembersBeforeSending(t *testing.T) {
	manager := peers.Options{}.Build(tg.NewClient(relayTestInvoker(func(context.Context, bin.Encoder, bin.Decoder) error { t.Fatal("unexpected RPC"); return nil })))
	elem := relayTestElem{from: manager.Channel(&tg.Channel{ID: 11}), to: manager.Channel(&tg.Channel{ID: 22}), msg: &tg.Message{ID: 42}, grouped: true}
	group := []*tg.Message{{ID: 44}, {ID: 42}, {ID: 43}}
	r := &Relay{Members: []int{42, 43, 44}}
	if err := r.Validate(elem, group); err != nil {
		t.Fatal(err)
	}
	for i, msg := range group {
		if msg.ID != 42+i {
			t.Fatal("album members are out of source order")
		}
	}
}
