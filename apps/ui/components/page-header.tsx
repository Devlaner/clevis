export function PageHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-8">
      <h1 className="text-3xl font-bold tracking-tight text-gradient">{title}</h1>
      <p className="mt-1 text-muted-foreground">{description}</p>
      <div className="mt-4 h-px bg-gradient-to-r from-primary/40 via-primary/10 to-transparent" />
    </div>
  )
}
