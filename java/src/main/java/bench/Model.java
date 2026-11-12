package bench;

import java.time.Instant;
import java.util.List;

/** The order JSON. Field names and order match the Go structs and the C# records, so all sides send the same bytes. */
final class Model {
    private Model() {}

    record Item(int line, String sku, int qty, long priceCents) {}

    record Order(long id, long customerId, String status, Instant createdAt, List<Item> items, long totalCents) {}
}
