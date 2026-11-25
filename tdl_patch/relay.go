// Telegram Forwarder Web extension to pinned TDL v0.20.4 (AGPL-3.0).
package forwarder

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"

	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
	"github.com/iyear/tdl/core/util/tutil"
)

// Relay records only API-confirmed results. No history/time-window matching.
type Relay struct {
	File       *os.File
	Token      string
	Actor      int64
	EditTarget int
	Members    []int
}

type relayReceipt struct {
	Version   int    `json:"version"`
	Token     string `json:"token"`
	Actor     int64  `json:"actor"`
	From      int64  `json:"from"`
	To        int64  `json:"to"`
	Source    int    `json:"source"`
	Target    int    `json:"target"`
	Operation string `json:"operation"`
}

func (r *Relay) Validate(elem Elem, grouped []*tg.Message) error {
	if elem.AsDryRun() || elem.Mode() != ModeClone || elem.From().ID() == elem.To().ID() {
		return fmt.Errorf("relay requires a real clone to a different channel")
	}
	if _, ok := elem.From().InputPeer().(*tg.InputPeerChannel); !ok {
		return fmt.Errorf("relay source must be a channel")
	}
	if _, ok := elem.To().InputPeer().(*tg.InputPeerChannel); !ok {
		return fmt.Errorf("relay destination must be a channel")
	}
	ids := []int{elem.Msg().ID}
	if len(grouped) > 0 {
		sort.Slice(grouped, func(i, j int) bool { return grouped[i].ID < grouped[j].ID })
		ids = nil
		for _, m := range grouped {
			ids = append(ids, m.ID)
		}
	}
	sort.Ints(ids)
	expected := append([]int(nil), r.Members...)
	sort.Ints(expected)
	if len(ids) == 0 || len(ids) > 10 || len(ids) != len(expected) {
		return fmt.Errorf("relay album membership changed")
	}
	for i, id := range ids {
		if id <= 0 || id != expected[i] || (i > 0 && id == ids[i-1]) {
			return fmt.Errorf("relay source IDs do not match the event")
		}
	}
	if r.EditTarget != 0 && (r.EditTarget < 0 || r.EditTarget >= 1<<31 || len(ids) != 1 || elem.AsGrouped()) {
		// Edits must explicitly use --single; never recreate an album.
		return fmt.Errorf("relay edit requires exactly one message with --single")
	}
	return nil
}

func receiptIDs(updates tg.UpdatesClass, random []int64) ([]int, error) {
	if short, ok := updates.(*tg.UpdateShortSentMessage); ok && len(random) == 1 && short.ID > 0 {
		return []int{short.ID}, nil
	}
	var list []tg.UpdateClass
	switch u := updates.(type) {
	case *tg.Updates:
		list = u.Updates
	case *tg.UpdatesCombined:
		list = u.Updates
	case *tg.UpdateShort:
		list = []tg.UpdateClass{u.Update}
	default:
		return nil, fmt.Errorf("unsupported receipt response %T", updates)
	}
	byRandom := map[int64]int{}
	for _, update := range list {
		if u, ok := update.(*tg.UpdateMessageID); ok {
			if previous, exists := byRandom[u.RandomID]; exists && previous != u.ID {
				return nil, fmt.Errorf("conflicting random_id receipt")
			}
			byRandom[u.RandomID] = u.ID
		}
	}
	ids := make([]int, len(random))
	seen := map[int]bool{}
	for i, randID := range random {
		id := byRandom[randID]
		if randID == 0 || id <= 0 || seen[id] {
			return nil, fmt.Errorf("incomplete API receipts")
		}
		ids[i], seen[id] = id, true
	}
	return ids, nil
}

func (r *Relay) Write(elem Elem, sources, targets []int, operation string) error {
	if r.File == nil || r.Actor <= 0 || len(sources) != len(targets) || len(sources) == 0 {
		return fmt.Errorf("invalid relay receipt sink")
	}
	for i, source := range sources {
		if source <= 0 || targets[i] <= 0 {
			return fmt.Errorf("invalid relay message ID")
		}
		v := relayReceipt{1, r.Token, r.Actor, elem.From().ID(), elem.To().ID(), source, targets[i], operation}
		if err := json.NewEncoder(r.File).Encode(v); err != nil {
			return err
		}
	}
	return r.File.Sync()
}

func (f *Forwarder) relaySent(elem Elem, updates tg.UpdatesClass, sources []int, random []int64) error {
	if f.opts.Relay == nil {
		return nil
	}
	ids, err := receiptIDs(updates, random)
	if err != nil {
		return err
	}
	return f.opts.Relay.Write(elem, sources, ids, "send")
}

func (f *Forwarder) relayEdit(ctx context.Context, elem Elem, convert func(*tg.Message) (tg.InputMediaClass, error)) error {
	r := f.opts.Relay
	msg := elem.Msg()
	client := f.forwardClient(ctx, elem)
	target, err := tutil.GetSingleMessage(ctx, client, elem.To().InputPeer(), r.EditTarget)
	if err != nil {
		return fmt.Errorf("read edit target: %w", err)
	}
	req := &tg.MessagesEditMessageRequest{Peer: elem.To().InputPeer(), ID: r.EditTarget}
	req.SetMessage(msg.Message)
	req.SetEntities(msg.Entities)
	switch msg.Media.(type) {
	case nil, *tg.MessageMediaWebPage:
		switch target.Media.(type) {
		case nil, *tg.MessageMediaWebPage:
		default:
			return fmt.Errorf("cannot remove existing target media by editing")
		}
	case *tg.MessageMediaPhoto, *tg.MessageMediaDocument:
		media, e := convert(msg)
		if e != nil {
			return e
		}
		req.SetMedia(media)
	default:
		return fmt.Errorf("unsupported edit media %T", msg.Media)
	}
	req.SetFlags()
	if _, err = client.MessagesEditMessage(ctx, req); err != nil && !tgerr.Is(err, "MESSAGE_NOT_MODIFIED") {
		return fmt.Errorf("edit existing message: %w", err)
	}
	return r.Write(elem, []int{msg.ID}, []int{r.EditTarget}, "edit")
}
