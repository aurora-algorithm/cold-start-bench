// The Go server: net/http with pattern routing and encoding/json, the standard library only.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"
)

const (
	maxID = 100_000
	lines = 10
)

// Field order and names match the Java records and the C# records, so every side sends the same bytes.
type Item struct {
	Line       int    `json:"line"`
	SKU        string `json:"sku"`
	Qty        int    `json:"qty"`
	PriceCents int64  `json:"priceCents"`
}

type Order struct {
	ID         int64     `json:"id"`
	CustomerID int64     `json:"customerId"`
	Status     string    `json:"status"`
	CreatedAt  time.Time `json:"createdAt"`
	Items      []Item    `json:"items"`
	TotalCents int64     `json:"totalCents"`
}

var (
	statuses = [...]string{"NEW", "PAID", "SHIPPED", "DELIVERED", "RETURNED"}
	epoch    = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
)

// sku pads by hand, as the Java side does, instead of fmt.Sprintf("SKU-%05d").
func sku(n int64) string {
	digits := strconv.FormatInt(n, 10)
	return "SKU-" + "00000"[len(digits):] + digits
}

func buildOrder(id int64) Order {
	items := make([]Item, 0, lines)
	var total int64
	for line := int64(0); line < lines; line++ {
		item := Item{
			Line:       int(line),
			SKU:        sku((id*31 + line) % 10000),
			Qty:        int((id+line)%5) + 1,
			PriceCents: (id*7+line*13)%5000 + 99,
		}
		total += int64(item.Qty) * item.PriceCents
		items = append(items, item)
	}
	return Order{id, (id*7919)%50000 + 1, statuses[id%5], epoch.Add(time.Duration(id%525600) * time.Minute), items, total}
}

func order(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || id < 1 || id > maxID {
		http.NotFound(w, r)
		return
	}
	body, err := json.Marshal(buildOrder(id))
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(body)
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "18080"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /orders/{id}", order)
	log.Fatal(http.ListenAndServe("127.0.0.1:"+port, mux))
}
