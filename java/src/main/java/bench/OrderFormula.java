package bench;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/**
 * Builds an order from its id with the same formulas as the Go and C# servers, so every side does the same
 * object and JSON work.
 */
final class OrderFormula {
    private static final String[] STATUSES = {"NEW", "PAID", "SHIPPED", "DELIVERED", "RETURNED"};
    private static final Instant EPOCH = Instant.parse("2026-01-01T00:00:00Z");
    static final int LINES = 10;

    private OrderFormula() {}

    /** Plain zero padding on both sides: String.format would make Java pay for a format parser; Go pads by hand too. */
    static String sku(long n) {
        String digits = Long.toString(n);
        return "SKU-" + "00000".substring(digits.length()) + digits;
    }

    static Model.Order build(long id) {
        List<Model.Item> items = new ArrayList<>(LINES);
        long total = 0;
        for (int line = 0; line < LINES; line++) {
            Model.Item item = new Model.Item(
                    line,
                    sku((id * 31 + line) % 10000),
                    (int) ((id + line) % 5) + 1,
                    (id * 7 + line * 13L) % 5000 + 99);
            total += item.qty() * item.priceCents();
            items.add(item);
        }
        return new Model.Order(id, (id * 7919) % 50000 + 1, STATUSES[(int) (id % 5)],
                EPOCH.plusSeconds(60 * (id % 525600)), items, total);
    }
}
