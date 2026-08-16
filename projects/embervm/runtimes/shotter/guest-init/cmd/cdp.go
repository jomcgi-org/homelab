package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"nhooyr.io/websocket"
)

const (
	// cdpTargetCreateTimeout and cdpTargetCloseTimeout bound the CDP HTTP
	// endpoint calls that open and close a fresh target. cdpCommandTimeout
	// bounds every other single CDP command (domain enable, device metrics,
	// screenshot capture, reading the final URL); Page.navigate and the
	// lifecycle wait that follows it use the caller's own navigateTimeout
	// instead, since that is the one the request controls, and Page.navigate
	// does not return until the navigation commits (response headers
	// arrive), so it needs that same caller-controlled budget too, not a
	// fixed internal one.
	cdpTargetCreateTimeout = 5 * time.Second
	cdpTargetCloseTimeout  = 5 * time.Second
	cdpCommandTimeout      = 10 * time.Second
)

// This file drives the already-warm Chromium over the Chrome DevTools
// Protocol. It talks to the CDP HTTP endpoint only to create and close a
// fresh target per invocation, and to the target's own websocket for every
// command and event. None of this is exercised by a Go unit test, since
// doing so needs a real browser; screenshot.go carries the request
// validation, bounds checking, and error mapping that a unit test can cover
// without one.

// cdpTarget is the subset of Chromium's /json/new response this handler uses.
type cdpTarget struct {
	ID                   string `json:"id"`
	WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
}

// createCDPTarget opens a fresh tab so a restored clone never resumes a page
// left over from a previous invocation, hours or hops apart. Chromium
// requires PUT rather than GET for the target-lifecycle endpoints, GET was
// deprecated because it let any page issue navigation-adjacent commands via
// a plain cross-origin image or link.
func createCDPTarget(ctx context.Context, httpBase string) (cdpTarget, error) {
	reqURL := httpBase + "/json/new?about:blank"
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, reqURL, nil)
	if err != nil {
		return cdpTarget{}, fmt.Errorf("build create-target request: %w", err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return cdpTarget{}, fmt.Errorf("create CDP target: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return cdpTarget{}, fmt.Errorf("create CDP target: status %s", resp.Status)
	}
	var target cdpTarget
	if err := json.NewDecoder(resp.Body).Decode(&target); err != nil {
		return cdpTarget{}, fmt.Errorf("decode create-target response: %w", err)
	}
	if target.ID == "" || target.WebSocketDebuggerURL == "" {
		return cdpTarget{}, errors.New("create-target response omitted id or websocket URL")
	}
	return target, nil
}

// closeCDPTarget always runs, on the success path and on every error path,
// so a long-lived warm browser never accumulates targets across restores.
// It logs rather than returns an error: by the time it runs, the response
// (or the failure) has already been decided, and a close failure should not
// override that outcome.
func closeCDPTarget(ctx context.Context, logger *slog.Logger, httpBase, targetID string) {
	reqURL := httpBase + "/json/close/" + targetID
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, reqURL, nil)
	if err != nil {
		logger.Warn("ember-shotter-init: build close-target request failed", "target", targetID, "err", err)
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		logger.Warn("ember-shotter-init: close CDP target failed", "target", targetID, "err", err)
		return
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		logger.Warn("ember-shotter-init: close CDP target returned non-200", "target", targetID, "status", resp.Status)
	}
}

// cdpMessage is the wire shape shared by CDP commands, command responses,
// and events. A response carries ID and either Result or Error; an event
// carries Method and Params with no ID.
type cdpMessage struct {
	ID     int64           `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Params json.RawMessage `json:"params,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *cdpError       `json:"error,omitempty"`
}

type cdpError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *cdpError) Error() string {
	return fmt.Sprintf("CDP error %d: %s", e.Code, e.Message)
}

// cdpSession is a single target's websocket connection. A background reader
// dispatches every inbound frame: replies keyed by ID go to whichever call
// is waiting on that ID, everything else (events) goes to an unbounded
// queue for waitForNavigation to drain.
//
// The queue is unbounded, not a fixed-size buffered channel with a
// non-blocking, drop-on-full send: Network.enable is on for the whole
// capture, so a hydrating page pushes a request/response/data-chunk event
// for every subresource through this one reader, and dropping
// Network.responseReceived for the main frame or Page.loadEventFired is
// never safe. The former silently reports status: 0 for a page that
// rendered fine; the latter hangs the caller to the full timeout. Neither
// is distinguishable from a real failure. One capture produces a bounded
// number of events (there is no long-lived multiplexing here, a fresh
// target lives for exactly one invocation), so an unbounded queue cannot
// run away, and it keeps the reader itself non-blocking: readLoop must
// never stall on a full channel, or it backs up the websocket's own read
// buffer and can wedge in-flight command responses too.
type cdpSession struct {
	conn    *websocket.Conn
	nextID  atomic.Int64
	mu      sync.Mutex
	pending map[int64]chan cdpMessage

	eventsMu     sync.Mutex
	eventsQueue  []cdpMessage
	eventsSignal chan struct{}
	eventsClosed bool
}

// dialCDPSession opens the per-target websocket. The capture rides this
// connection base64-encoded inside a JSON envelope (CDP's own "data" field,
// plus id/result wrapping), so the read limit is maxEncodedPNGBytes, the
// ceiling this handler actually enforces on that same encoded payload, plus
// slack for CDP's own JSON wrapper around it.
func dialCDPSession(ctx context.Context, wsURL string) (*cdpSession, error) {
	conn, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		return nil, fmt.Errorf("dial CDP websocket: %w", err)
	}
	conn.SetReadLimit(int64(maxEncodedPNGBytes) + (4 << 20))

	session := &cdpSession{
		conn:         conn,
		pending:      make(map[int64]chan cdpMessage),
		eventsSignal: make(chan struct{}, 1),
	}
	go session.readLoop()
	return session, nil
}

func (s *cdpSession) readLoop() {
	defer s.closeEvents()
	for {
		_, data, err := s.conn.Read(context.Background())
		if err != nil {
			s.mu.Lock()
			for id, ch := range s.pending {
				close(ch)
				delete(s.pending, id)
			}
			s.mu.Unlock()
			return
		}
		var msg cdpMessage
		if err := json.Unmarshal(data, &msg); err != nil {
			continue
		}
		if msg.ID != 0 {
			s.mu.Lock()
			ch, ok := s.pending[msg.ID]
			if ok {
				delete(s.pending, msg.ID)
			}
			s.mu.Unlock()
			if ok {
				ch <- msg
				close(ch)
			}
			continue
		}
		s.pushEvent(msg)
	}
}

// pushEvent appends to the unbounded queue and wakes at most one waiter.
// It never blocks, so a slow or absent consumer can never stall readLoop.
func (s *cdpSession) pushEvent(msg cdpMessage) {
	s.eventsMu.Lock()
	s.eventsQueue = append(s.eventsQueue, msg)
	s.eventsMu.Unlock()
	s.signalEvents()
}

func (s *cdpSession) signalEvents() {
	select {
	case s.eventsSignal <- struct{}{}:
	default:
	}
}

func (s *cdpSession) closeEvents() {
	s.eventsMu.Lock()
	s.eventsClosed = true
	s.eventsMu.Unlock()
	s.signalEvents()
}

// drainEvents discards every event queued so far without blocking. Used
// between Page.enable and Page.navigate: a stray lifecycle event left over
// from the fresh about:blank target the CDP target was created with must
// not be able to satisfy waitForNavigation immediately and yield a
// blank-white screenshot with status: 0.
func (s *cdpSession) drainEvents() {
	s.eventsMu.Lock()
	s.eventsQueue = nil
	s.eventsMu.Unlock()
	select {
	case <-s.eventsSignal:
	default:
	}
}

// nextEvent blocks until an event is queued, the session closes, or ctx is
// done. ok is false only when the queue is drained and the session has
// closed; a ctx timeout returns a non-nil err instead.
func (s *cdpSession) nextEvent(ctx context.Context) (msg cdpMessage, ok bool, err error) {
	for {
		s.eventsMu.Lock()
		if len(s.eventsQueue) > 0 {
			msg = s.eventsQueue[0]
			s.eventsQueue = s.eventsQueue[1:]
			s.eventsMu.Unlock()
			return msg, true, nil
		}
		closed := s.eventsClosed
		s.eventsMu.Unlock()
		if closed {
			return cdpMessage{}, false, nil
		}

		select {
		case <-s.eventsSignal:
			continue
		case <-ctx.Done():
			return cdpMessage{}, false, ctx.Err()
		}
	}
}

func (s *cdpSession) close() {
	_ = s.conn.Close(websocket.StatusNormalClosure, "")
}

func (s *cdpSession) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	id := s.nextID.Add(1)
	payload := cdpMessage{ID: id, Method: method}
	if params != nil {
		raw, err := json.Marshal(params)
		if err != nil {
			return nil, fmt.Errorf("marshal params for %s: %w", method, err)
		}
		payload.Params = raw
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal command %s: %w", method, err)
	}

	respCh := make(chan cdpMessage, 1)
	s.mu.Lock()
	s.pending[id] = respCh
	s.mu.Unlock()

	if err := s.conn.Write(ctx, websocket.MessageText, data); err != nil {
		s.mu.Lock()
		delete(s.pending, id)
		s.mu.Unlock()
		return nil, fmt.Errorf("write command %s: %w", method, err)
	}

	select {
	case msg, ok := <-respCh:
		if !ok {
			return nil, fmt.Errorf("CDP connection closed waiting for %s", method)
		}
		if msg.Error != nil {
			return nil, fmt.Errorf("%s: %w", method, msg.Error)
		}
		return msg.Result, nil
	case <-ctx.Done():
		s.mu.Lock()
		delete(s.pending, id)
		s.mu.Unlock()
		return nil, ctx.Err()
	}
}

// navigate sends Page.navigate and returns the frameId of the navigated
// frame, used to match the main document's response status and to ignore
// events from any iframe the page happens to load alongside it.
func (s *cdpSession) navigate(ctx context.Context, url string) (frameID string, err error) {
	result, err := s.call(ctx, "Page.navigate", map[string]string{"url": url})
	if err != nil {
		return "", fmt.Errorf("%w: %v", errNavigationFailed, err)
	}
	var nav struct {
		FrameID   string `json:"frameId"`
		ErrorText string `json:"errorText"`
	}
	if err := json.Unmarshal(result, &nav); err != nil {
		return "", fmt.Errorf("%w: decode navigate response: %v", errNavigationFailed, err)
	}
	if nav.ErrorText != "" {
		return "", fmt.Errorf("%w: %s", errNavigationFailed, nav.ErrorText)
	}
	return nav.FrameID, nil
}

// waitForNavigation blocks until the configured lifecycle event fires for
// the main frame, tracking the most recent main-frame document response
// status seen along the way (the last one wins, so a redirect chain reports
// the final response, matching finalURL semantics).
func (s *cdpSession) waitForNavigation(ctx context.Context, lifecycleEvent, mainFrameID string) (documentStatus int, err error) {
	for {
		msg, ok, err := s.nextEvent(ctx)
		if err != nil {
			return documentStatus, fmt.Errorf("%w: %v", errNavigationTimeout, err)
		}
		if !ok {
			return documentStatus, errors.New("CDP connection closed while waiting for navigation")
		}
		switch msg.Method {
		case "Network.responseReceived":
			var event struct {
				FrameID  string `json:"frameId"`
				Type     string `json:"type"`
				Response struct {
					Status int `json:"status"`
				} `json:"response"`
			}
			if err := json.Unmarshal(msg.Params, &event); err == nil &&
				event.Type == "Document" && event.FrameID == mainFrameID {
				documentStatus = event.Response.Status
			}
		case lifecycleEvent:
			return documentStatus, nil
		}
	}
}

// contentSize reads the page's full scrollable size for a full-page capture.
func (s *cdpSession) contentSize(ctx context.Context) (width, height int, err error) {
	result, callErr := s.call(ctx, "Page.getLayoutMetrics", nil)
	if callErr != nil {
		return 0, 0, fmt.Errorf("get layout metrics: %w", callErr)
	}
	var metrics struct {
		CSSContentSize struct {
			Width  float64 `json:"width"`
			Height float64 `json:"height"`
		} `json:"cssContentSize"`
	}
	if err := json.Unmarshal(result, &metrics); err != nil {
		return 0, 0, fmt.Errorf("decode layout metrics: %w", err)
	}
	return int(metrics.CSSContentSize.Width), int(metrics.CSSContentSize.Height), nil
}

// captureScreenshot returns the PNG bytes plus the dimensions actually
// captured. For a full-page capture those dimensions are the page's content
// size, not the requested viewport, so the response has to report what was
// actually produced rather than echo the request back.
func (s *cdpSession) captureScreenshot(ctx context.Context, fullPage bool, viewportWidth, viewportHeight int) (png []byte, width, height int, err error) {
	params := map[string]any{"format": "png"}
	width, height = viewportWidth, viewportHeight

	if fullPage {
		contentWidth, contentHeight, sizeErr := s.contentSize(ctx)
		if sizeErr != nil {
			return nil, 0, 0, sizeErr
		}
		if contentWidth <= 0 {
			contentWidth = viewportWidth
		}
		if contentHeight <= 0 {
			contentHeight = viewportHeight
		}
		if contentWidth > maxDimension || contentHeight > maxDimension {
			return nil, 0, 0, fmt.Errorf("%w: full-page content is %dx%d, exceeds the %dx%d cap", errCaptureTooLarge, contentWidth, contentHeight, maxDimension, maxDimension)
		}
		width, height = contentWidth, contentHeight
		params["captureBeyondViewport"] = true
		params["clip"] = map[string]any{
			"x": 0, "y": 0,
			"width": contentWidth, "height": contentHeight,
			"scale": 1,
		}
	}

	result, err := s.call(ctx, "Page.captureScreenshot", params)
	if err != nil {
		return nil, 0, 0, fmt.Errorf("capture screenshot: %w", err)
	}
	var capture struct {
		Data string `json:"data"`
	}
	if err := json.Unmarshal(result, &capture); err != nil {
		return nil, 0, 0, fmt.Errorf("decode capture response: %w", err)
	}
	decoded, err := base64.StdEncoding.DecodeString(capture.Data)
	if err != nil {
		return nil, 0, 0, fmt.Errorf("decode capture base64: %w", err)
	}
	// Checked against the ENCODED length this handler will actually produce
	// when it re-encodes decoded for its own response (screenshotResponse.
	// PNGBase64), not the decoded length: what crosses the transport is the
	// encoded bytes inside a JSON envelope, and that is what has to stay
	// under the workload's resultMaxBytes with real headroom to spare.
	if encodedLen := base64.StdEncoding.EncodedLen(len(decoded)); encodedLen > maxEncodedPNGBytes {
		return nil, 0, 0, fmt.Errorf("%w: base64-encoded PNG is %d bytes, exceeds the %d byte cap", errCaptureTooLarge, encodedLen, maxEncodedPNGBytes)
	}
	return decoded, width, height, nil
}

// currentURL reads window.location.href after navigation settles, which is
// the final URL after any client- or server-side redirect.
func (s *cdpSession) currentURL(ctx context.Context) (string, error) {
	result, err := s.call(ctx, "Runtime.evaluate", map[string]any{
		"expression":    "location.href",
		"returnByValue": true,
	})
	if err != nil {
		return "", fmt.Errorf("evaluate location.href: %w", err)
	}
	var evalResult struct {
		Result struct {
			Value string `json:"value"`
		} `json:"result"`
	}
	if err := json.Unmarshal(result, &evalResult); err != nil {
		return "", fmt.Errorf("decode location.href result: %w", err)
	}
	return evalResult.Result.Value, nil
}

// captureScreenshotViaCDP drives one full invocation against a fresh target:
// create, set up, navigate, wait, capture, read the final URL, close. The
// target close is deferred immediately after creation so it runs on every
// return path, including every error below this point.
func captureScreenshotViaCDP(ctx context.Context, logger *slog.Logger, cdpHTTPBase string, req validatedScreenshot) (screenshotResponse, error) {
	start := time.Now()

	createCtx, cancelCreate := context.WithTimeout(ctx, cdpTargetCreateTimeout)
	target, err := createCDPTarget(createCtx, cdpHTTPBase)
	cancelCreate()
	if err != nil {
		return screenshotResponse{}, fmt.Errorf("open fresh CDP target: %w", err)
	}
	defer func() {
		closeCtx, cancelClose := context.WithTimeout(context.Background(), cdpTargetCloseTimeout)
		closeCDPTarget(closeCtx, logger, cdpHTTPBase, target.ID)
		cancelClose()
	}()

	dialCtx, cancelDial := context.WithTimeout(ctx, cdpTargetCreateTimeout)
	session, err := dialCDPSession(dialCtx, target.WebSocketDebuggerURL)
	cancelDial()
	if err != nil {
		return screenshotResponse{}, fmt.Errorf("connect to fresh CDP target: %w", err)
	}
	defer session.close()

	setupCtx, cancelSetup := context.WithTimeout(ctx, cdpCommandTimeout)
	defer cancelSetup()
	if _, err := session.call(setupCtx, "Page.enable", nil); err != nil {
		return screenshotResponse{}, fmt.Errorf("enable Page domain: %w", err)
	}
	if _, err := session.call(setupCtx, "Network.enable", nil); err != nil {
		return screenshotResponse{}, fmt.Errorf("enable Network domain: %w", err)
	}
	if _, err := session.call(setupCtx, "Emulation.setDeviceMetricsOverride", map[string]any{
		"width": req.width, "height": req.height,
		"deviceScaleFactor": 1, "mobile": false,
	}); err != nil {
		return screenshotResponse{}, fmt.Errorf("set device metrics: %w", err)
	}

	// Drop anything queued between target creation and here, most notably a
	// stray lifecycle event from the fresh about:blank target Chromium opens
	// on every new target. Without this, that leftover event can satisfy
	// waitForNavigation the instant it starts listening, before Page.navigate
	// has even sent, yielding a blank-white screenshot with status: 0.
	session.drainEvents()

	// navCtx bounds both Page.navigate and the lifecycle wait that follows it
	// with the same budget: the caller's own navigateTimeout, not a fixed
	// internal constant. Page.navigate does not return until the navigation
	// commits (response headers arrive), so a slow server's time to first
	// byte has to fit inside the budget the caller actually asked for, not a
	// fixed cdpCommandTimeout that has no relationship to it.
	navCtx, cancelNav := context.WithTimeout(ctx, req.navigateTimeout)
	defer cancelNav()

	mainFrameID, err := session.navigate(navCtx, req.navigateURL)
	if err != nil {
		return screenshotResponse{}, err
	}

	status, err := session.waitForNavigation(navCtx, req.waitUntilEvent, mainFrameID)
	if err != nil {
		return screenshotResponse{}, err
	}

	captureCtx, cancelCapture := context.WithTimeout(ctx, cdpCommandTimeout)
	defer cancelCapture()
	png, width, height, err := session.captureScreenshot(captureCtx, req.fullPage, req.width, req.height)
	if err != nil {
		return screenshotResponse{}, err
	}

	finalURL, err := session.currentURL(captureCtx)
	if err != nil {
		logger.Warn("ember-shotter-init: could not read final URL after navigation", "err", err)
		finalURL = req.navigateURL
	}

	return screenshotResponse{
		PNGBase64:  base64.StdEncoding.EncodeToString(png),
		Width:      width,
		Height:     height,
		FinalURL:   finalURL,
		Status:     status,
		DurationMs: time.Since(start).Milliseconds(),
	}, nil
}
