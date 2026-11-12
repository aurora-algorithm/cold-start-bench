using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Http.HttpResults;

var port = Environment.GetEnvironmentVariable("PORT") ?? "18080";

var builder = WebApplication.CreateSlimBuilder(args);
builder.Logging.SetMinimumLevel(LogLevel.Warning);
builder.Services.ConfigureHttpJsonOptions(o => o.SerializerOptions.TypeInfoResolverChain.Insert(0, AppJson.Default));
var app = builder.Build();

app.MapGet("/orders/{id:long}", Orders.Get);

app.Run($"http://127.0.0.1:{port}");

public sealed record Item(int Line, string Sku, int Qty, long PriceCents);

public sealed record Order(long Id, long CustomerId, string Status, DateTime CreatedAt, List<Item> Items, long TotalCents);

[JsonSourceGenerationOptions(PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase)]
[JsonSerializable(typeof(Order))]
internal partial class AppJson : JsonSerializerContext;

public static class Orders
{
    private const long MaxId = 100_000;
    private const int Lines = 10;
    private static readonly string[] Statuses = ["NEW", "PAID", "SHIPPED", "DELIVERED", "RETURNED"];
    private static readonly DateTime Epoch = new(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    public static Results<Ok<Order>, NotFound> Get(long id) =>
        id is < 1 or > MaxId ? TypedResults.NotFound() : TypedResults.Ok(Build(id));

    // Plain zero padding, as the Java and Go sides do.
    private static string Sku(long n)
    {
        var digits = n.ToString(System.Globalization.CultureInfo.InvariantCulture);
        return "SKU-" + "00000"[digits.Length..] + digits;
    }

    private static Order Build(long id)
    {
        var items = new List<Item>(Lines);
        long total = 0;
        for (var line = 0; line < Lines; line++)
        {
            var item = new Item(line, Sku((id * 31 + line) % 10000), (int)((id + line) % 5) + 1, (id * 7 + line * 13L) % 5000 + 99);
            total += item.Qty * item.PriceCents;
            items.Add(item);
        }
        return new Order(id, id * 7919 % 50000 + 1, Statuses[id % 5], Epoch.AddSeconds(60 * (id % 525600)), items, total);
    }
}
